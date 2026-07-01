"""Smoke tests for the ODM revenue block.

Fixture-only — no feed load, no network. Verifies:

  1. `compute_odm_revenue` sums demand-weighted deltas across every
     `AffectedFare`'s blast-radius pairs, honouring the ticket-aware match
     when the ODM has it and falling back to (o,d) aggregate otherwise.
  2. `load_odm_index` tolerates ORR column-naming drift and de-dupes rows
     summing by (o,d).
  3. The block degrades gracefully to `None` + an honest note when the CSV
     is missing.
  4. Compute always emits the ESTIMATE caveat in notes[0].

The heavy real-ODM sweep (1.3M rows, cross-check against ORR aggregates) is
a separate session."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact.affected import AffectedFare, AffectedSet
from src.impact.odm import (
    ODMIndex,
    compute_odm_revenue,
    load_odm_index,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ODM_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "odm"


# --- Helpers --------------------------------------------------------------


def _fare(
    *,
    flow_id: str,
    ticket_code: str,
    old_pence: int,
    new_pence: int,
    blast_pairs: tuple[tuple[str, str], ...],
) -> AffectedFare:
    return AffectedFare(
        flow_id=flow_id,
        ticket_code=ticket_code,
        route_code="00000",
        representative_origin_nlc=blast_pairs[0][0],
        representative_dest_nlc=blast_pairs[0][1],
        status="resolved",
        old_price_pence=old_pence,
        new_price_pence=new_pence,
        discount_category="01",
        provenance=tuple(),
        blast_radius_pairs=blast_pairs,
    )


def _affected(*rows: AffectedFare) -> AffectedSet:
    return AffectedSet(
        canonical=rows,
        skipped=tuple(),
        blast_radius=tuple(),
        notes=tuple(),
    )


# --- 1. Loader shape ------------------------------------------------------


def test_load_odm_index_sums_duplicate_pairs() -> None:
    """The mini fixture has (2968,1072) twice — 1000 + 2000; loader must sum."""
    idx = load_odm_index(ODM_FIXTURE_DIR / "mini_odm.csv")
    assert idx.by_pair[("2968", "1444")] == 5000
    assert idx.by_pair[("2968", "1072")] == 3000
    assert idx.is_ticket_aware is False
    assert idx.row_count == 5


def test_load_odm_index_detects_ticket_aware() -> None:
    idx = load_odm_index(ODM_FIXTURE_DIR / "ticket_aware_odm.csv")
    assert idx.is_ticket_aware is True
    assert idx.by_pair_and_ticket[("2968", "1444", "SOR")] == 3000
    # Ticket-aware also populates the aggregate view for fall-back matches.
    assert idx.by_pair[("2968", "1444")] == 3000 + 1500 + 500


# --- 2. Weighted-delta computation ---------------------------------------


def test_odm_block_computes_weighted_delta_on_fixture() -> None:
    """Hand-computed expected values.

    Fare A: SOR, MAN(2968)->EUS(1444), delta = -300p, blast radius = 1 pair
            match: 5000 journeys × -300p = -1,500,000p
    Fare B: SDS, MAN(2968)->London Terms(1072), delta = -100p, blast radius =
            2 pairs but one is an unmatched cluster expansion (2968, 1444)
            already covered above, so we make it a distinct (2968, 1072) +
            (2969, 1444). 2968->1072 = 3000j × -100 = -300,000p. 2969->1444 =
            750j × -100 = -75,000p. Total for B = -375,000p.
    Fare C: SDR, unmatched pair (5555, 6666), 0 journeys → 0 contribution."""
    idx = load_odm_index(ODM_FIXTURE_DIR / "mini_odm.csv")
    a = _fare(
        flow_id="F001", ticket_code="SOR",
        old_pence=10000, new_pence=9700,
        blast_pairs=(("2968", "1444"),),
    )
    b = _fare(
        flow_id="F002", ticket_code="SDS",
        old_pence=5000, new_pence=4900,
        blast_pairs=(("2968", "1072"), ("2969", "1444")),
    )
    c = _fare(
        flow_id="F003", ticket_code="SDR",
        old_pence=8000, new_pence=7500,
        blast_pairs=(("5555", "6666"),),
    )
    block = compute_odm_revenue(_affected(a, b, c), idx)

    assert block.matched_flow_count == 2  # A and B
    assert block.unmatched_flow_count == 1  # C
    est_a = next(e for e in block.estimates if e.flow_id == "F001")
    assert est_a.revenue_delta_pence == -1_500_000
    assert est_a.matched_pair_count == 1
    assert est_a.unmatched_pair_count == 0
    est_b = next(e for e in block.estimates if e.flow_id == "F002")
    assert est_b.revenue_delta_pence == -375_000
    assert est_b.matched_pair_count == 2
    est_c = next(e for e in block.estimates if e.flow_id == "F003")
    assert est_c.revenue_delta_pence == 0
    assert est_c.matched_pair_count == 0
    assert est_c.unmatched_pair_count == 1

    assert block.total_revenue_delta_pence == -1_500_000 + -375_000 + 0
    # ESTIMATE caveat must be the first note verbatim (LLM/UI contract).
    assert block.notes[0].startswith("ESTIMATE")
    # Unmatched flow count also surfaced.
    assert any("1/3" in n and "coverage" in n for n in block.notes)


def test_odm_block_prefers_ticket_specific_row_when_available() -> None:
    """Ticket-aware ODM: (2968,1444,SOR) has 3000 journeys, but the aggregate
    (2968,1444) is 5000. The block MUST pick the ticket-specific 3000."""
    idx = load_odm_index(ODM_FIXTURE_DIR / "ticket_aware_odm.csv")
    fare = _fare(
        flow_id="F001", ticket_code="SOR",
        old_pence=10000, new_pence=9700,
        blast_pairs=(("2968", "1444"),),
    )
    block = compute_odm_revenue(_affected(fare), idx)
    est = block.estimates[0]
    assert est.journeys_per_period == 3000
    assert est.revenue_delta_pence == 3000 * -300
    # No ticket-aggregation note — the specific match was found.
    assert not any("ticket-aggregated" in n for n in block.notes)


def test_odm_block_falls_back_to_pair_aggregate_when_ticket_missing() -> None:
    """Ticket-aware ODM but this ticket_code isn't in it — must fall back to
    the (o,d) aggregate + emit the ticket-aggregation note."""
    idx = load_odm_index(ODM_FIXTURE_DIR / "ticket_aware_odm.csv")
    fare = _fare(
        flow_id="F001", ticket_code="ZZZ",  # not in the ticket-aware fixture
        old_pence=10000, new_pence=9700,
        blast_pairs=(("2968", "1444"),),
    )
    block = compute_odm_revenue(_affected(fare), idx)
    est = block.estimates[0]
    assert est.journeys_per_period == 5000  # the (2968,1444) aggregate
    assert any("ticket-aggregated" in n for n in block.notes)


# --- 3. Loader validation --------------------------------------------------


def test_load_odm_index_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="ODM CSV not found"):
        load_odm_index(REPO_ROOT / "tests" / "fixtures" / "odm" / "nope.csv")


def test_load_odm_index_missing_columns_raises(tmp_path: Path) -> None:
    """Loudly refuse to guess the demand column."""
    bad = tmp_path / "bad.csv"
    bad.write_text("origin_nlc,dest_nlc\n2968,1444\n", encoding="utf-8")
    with pytest.raises(ValueError, match="journey/demand"):
        load_odm_index(bad)


# --- 4. Empty-affected edge case ------------------------------------------


def test_odm_block_on_empty_affected_set() -> None:
    """No canonical rows → empty estimates, zero total, still emits caveat."""
    idx = ODMIndex(
        by_pair={}, by_pair_and_ticket={},
        period_label="test", is_ticket_aware=False, row_count=0,
        notes=tuple(),
    )
    block = compute_odm_revenue(_affected(), idx)
    assert block.estimates == tuple()
    assert block.total_revenue_delta_pence == 0
    assert block.matched_flow_count == 0
    assert block.unmatched_flow_count == 0
    assert block.notes[0].startswith("ESTIMATE")
