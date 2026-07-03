"""Frozen demand-module gates (see docs/demand-carbon-validation.md).

Fast tests freeze the D1 worked examples and the formula-level D2
invariants — pure arithmetic over the published elasticity table, no
feed. The slow block freezes the engine-level D2/D3 gates against the
real feed + the ODM fixture, exactly as the validator ran them.

The C3 oracle gate lives in tests/test_carbon.py (frozen 2026-07-03 for
the corridors that passed against the RDG Green Travel Data calculator).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.impact import ChangeRequest, FeedPaths, compute_impact
from src.impact.elasticities import (
    ELASTICITIES,
    Direction,
    FlowType,
    TicketSegment,
    lookup_elasticity,
)
from src.impact.odm import load_odm_index
from src.ingest.inspect import load_loc_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
ODM_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "odm" / "mini_odm.csv"


# --- D1: hand-computed worked examples (gate-frozen fixtures) ---------------

# (name, flow_type, segment, direction, published eps, price ratio,
#  hand-computed response %). Computed by hand once — NOT by this test.
D1_CASES = (
    ("TS commuting +10% rise", FlowType.NETWORK_LONDON, TicketSegment.SEASON,
     Direction.INCREASE, -0.641, 1.10, -5.93),
    ("TS commuting -10% cut", FlowType.NETWORK_LONDON, TicketSegment.SEASON,
     Direction.REDUCTION, -0.144, 0.90, +1.53),
    ("PDFH LD-London +5% rise", FlowType.LD_LONDON, TicketSegment.NON_SEASON,
     Direction.INCREASE, -0.95, 1.05, -4.53),
)


@pytest.mark.parametrize("name,ft,seg,direction,want_eps,ratio,want_pct",
                         D1_CASES, ids=[c[0] for c in D1_CASES])
def test_d1_worked_examples(name, ft, seg, direction, want_eps, ratio,
                            want_pct) -> None:
    eps = lookup_elasticity(ft, seg, direction)
    assert eps.value == pytest.approx(want_eps, abs=1e-9), (
        f"{name}: elasticity cell drifted from the published value")
    got_pct = (ratio ** eps.value - 1.0) * 100.0
    assert got_pct == pytest.approx(want_pct, abs=0.01)


# --- D2: formula-level invariants (all cells, no feed) -----------------------


def test_d2_table_is_total() -> None:
    """Every (flow_type, segment, direction) cell exists — lookup can't KeyError."""
    assert len(ELASTICITIES) == 16
    for ft in FlowType:
        for seg in TicketSegment:
            for d in Direction:
                assert lookup_elasticity(ft, seg, d).value < 0


def test_d2_sign_all_cells() -> None:
    """A rise loses demand, a cut gains — every cell."""
    for (ft, seg, d), e in ELASTICITIES.items():
        if d == Direction.INCREASE:
            assert 1.10 ** e.value < 1.0, (ft, seg, d)
        else:
            assert 0.90 ** e.value > 1.0, (ft, seg, d)


def test_d2_asymmetry_all_segments() -> None:
    """A 10% cut gains less than a 10% rise loses, per segment (the
    published increase-side elasticities are stronger than reduction-side)."""
    for ft in FlowType:
        for seg in TicketSegment:
            up = lookup_elasticity(ft, seg, Direction.INCREASE).value
            down = lookup_elasticity(ft, seg, Direction.REDUCTION).value
            loss = 1.0 - 1.10 ** up
            gain = 0.90 ** down - 1.0
            assert gain < loss, (ft, seg)


# --- Engine-level gates against the real feed (slow) -------------------------


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return dataclasses.replace(paths, odm_csv=ODM_FIXTURE)


def _demo_change(discount_pct: float) -> ChangeRequest:
    return ChangeRequest(
        kind="add_railcard", railcard_code="STU", discount_pct=discount_pct,
        discount_categories=("01",), corridor_origin_nlc="2968",
        corridor_dest_nlc="1444", peak_valid=True,
        description="demand freeze test")


@pytest.fixture(scope="module")
def demo_demand(feed_paths: FeedPaths):
    report = compute_impact(_demo_change(1.0 / 3.0), feed_paths,
                            include={"demand"})
    assert report.demand is not None
    return report.demand


@pytest.mark.slow
def test_d2_zero_change_rejected_at_boundary(feed_paths: FeedPaths) -> None:
    with pytest.raises(ValueError):
        compute_impact(_demo_change(0.0), feed_paths, include={"demand"})


@pytest.mark.slow
def test_d2_volume_invariants(demo_demand) -> None:
    """abstraction <= existing volume, net <= gross, on every volume row."""
    vol_rows = [e for e in demo_demand.estimates
                if e.net_new_journeys is not None]
    assert vol_rows, "fixture ODM matched no demo flow"
    for e in vol_rows:
        assert e.abstracted_journeys <= e.odm_journeys_per_period
        assert e.abstracted_journeys <= e.eligible_base_journeys
        assert e.net_new_journeys <= e.gross_product_journeys


@pytest.mark.slow
def test_d2_demo_trips_validity_warning(demo_demand) -> None:
    """The ~33% demo cut exceeds the ±25% band on every row."""
    assert demo_demand.validity_warnings == len(demo_demand.estimates)
    for e in demo_demand.estimates:
        assert abs(e.price_change_pct) > 25.0
        assert not e.within_validity


@pytest.mark.slow
def test_d2_all_demo_rows_route_to_reduction(demo_demand) -> None:
    for e in demo_demand.estimates:
        assert e.direction == "reduction"
        assert e.gross_demand_change_pct > 0.0


@pytest.mark.slow
def test_d2_implied_yield_used_as_price_base(feed_paths: FeedPaths,
                                             tmp_path: Path) -> None:
    """An ODM with a revenue column drives yield = revenue/journeys, and
    demand rows adopt it as the price base."""
    csv = tmp_path / "odm_with_revenue.csv"
    csv.write_text("origin_nlc,dest_nlc,journeys_per_year,revenue_pence\n"
                   "2968,1444,5000,45000000\n", encoding="utf-8")
    loc = load_loc_meta(feed_paths.loc)
    odm = load_odm_index(csv, loc=loc)
    assert odm.yield_pence("2968", "1444") == 9000

    report = compute_impact(_demo_change(1.0 / 3.0),
                            dataclasses.replace(feed_paths, odm_csv=csv),
                            include={"demand"})
    assert report.demand is not None
    yield_rows = [e for e in report.demand.estimates
                  if e.yield_basis == "odm_yield"]
    assert yield_rows
    for e in yield_rows:
        assert e.price_base_pence == 9000


@pytest.mark.slow
def test_d3_demo_growth_in_elasticity_band(demo_demand) -> None:
    """Gate D3: predicted gross growth for the demo cut lands in
    [1%, 30%] (single digits to low tens), pre-registered band."""
    rows = [e for e in demo_demand.estimates if e.direction == "reduction"]
    assert rows
    assert min(e.gross_demand_change_pct for e in rows) >= 1.0
    assert max(e.gross_demand_change_pct for e in rows) <= 30.0


@pytest.mark.slow
def test_demand_block_is_labelled_estimate(demo_demand) -> None:
    assert demo_demand.notes and demo_demand.notes[0].startswith("ESTIMATE")
