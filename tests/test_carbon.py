"""Frozen carbon-module gates (see docs/demand-carbon-validation.md).

Fast tests freeze C1 — the National Rail calculator worked example and
the DEFRA blend identity, pure arithmetic over the hand-encoded factor
constants. The slow block freezes C2 (distance + traction) and the
engine-level carbon block against the real feed, exactly as the
validator ran them.

The C3 oracle gate is NOT frozen here — it is still SKIP in the
validation report until data/carbon_oracle_template.json is hand-filled.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.impact import ChangeRequest, FeedPaths, compute_impact
from src.impact.carbon_factors import (
    CAR_AVG_OCCUPANCY,
    CAR_KGCO2E_PER_VKM,
    RAIL_DIESEL_KGCO2E_PER_PKM,
    RAIL_ELECTRIC_KGCO2E_PER_PKM,
    RAIL_NATIONAL_KGCO2E_PER_PKM,
    car_factor_per_passenger_km,
    rail_factor_for_mix,
)
from src.impact.distance import flow_distance_km

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
ODM_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "odm" / "mini_odm.csv"

# National Rail carbon-calculator substantiation worked example
# (37.3 km journey), the C1 reference values. Tolerance 1% — the
# published figures are rounded to 2 dp.
NR_EXAMPLE_KM = 37.3
NR_RAIL_KG = 1.32
NR_CAR_KG = 4.14
NR_SAVING_KG = 2.82
C1_TOL = 0.01


# --- C1: published worked-example arithmetic (gate-frozen) -------------------


def test_c1_nr_worked_example_rail() -> None:
    got = NR_EXAMPLE_KM * RAIL_NATIONAL_KGCO2E_PER_PKM
    assert got == pytest.approx(NR_RAIL_KG, rel=C1_TOL)


def test_c1_nr_worked_example_car() -> None:
    got = NR_EXAMPLE_KM * CAR_KGCO2E_PER_VKM / CAR_AVG_OCCUPANCY
    assert got == pytest.approx(NR_CAR_KG, rel=C1_TOL)
    assert got == pytest.approx(NR_EXAMPLE_KM * car_factor_per_passenger_km())


def test_c1_nr_worked_example_saving() -> None:
    rail = NR_EXAMPLE_KM * RAIL_NATIONAL_KGCO2E_PER_PKM
    car = NR_EXAMPLE_KM * CAR_KGCO2E_PER_VKM / CAR_AVG_OCCUPANCY
    assert car - rail == pytest.approx(NR_SAVING_KG, rel=C1_TOL)


def test_c1_defra_blend_identity() -> None:
    """The DERIVED electric/diesel pair must reproduce the DEFRA national
    average at the published ~70/30 national traction split."""
    factor, desc = rail_factor_for_mix(0.70, 0.30, 0.0)
    assert factor == pytest.approx(RAIL_NATIONAL_KGCO2E_PER_PKM, rel=C1_TOL)
    assert "DERIVED" in desc


def test_c1_factor_ordering() -> None:
    """Electric < national average < diesel, and an all-unknown mix falls
    back to exactly the national average (disclosed in the description)."""
    assert (RAIL_ELECTRIC_KGCO2E_PER_PKM
            < RAIL_NATIONAL_KGCO2E_PER_PKM
            < RAIL_DIESEL_KGCO2E_PER_PKM)
    factor, desc = rail_factor_for_mix(0.0, 0.0, 1.0)
    assert factor == RAIL_NATIONAL_KGCO2E_PER_PKM
    assert "unknown" in desc


# --- Engine-level gates against the real feed (slow) -------------------------

C2_DIST_REF_KM = 296.0
C2_DIST_TOL = 0.05
C2_TRACTION_MIN_SHARE = 0.90


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return dataclasses.replace(paths, odm_csv=ODM_FIXTURE)


@pytest.fixture(scope="module")
def msn_path() -> Path | None:
    from src.api.geo import default_msn_path

    return default_msn_path(DATA)


@pytest.mark.slow
def test_c2_man_eus_distance_is_rgd_mileage(feed_paths: FeedPaths,
                                            msn_path: Path | None) -> None:
    """Gate C2: MAN->EUS within ±5% of the 296 km reference, and via the
    real routeing-guide graph, not the great-circle approximation."""
    if feed_paths.rgd is None or not feed_paths.rgd.exists():
        pytest.skip("no .RGD station-link file in data/")
    dist = flow_distance_km("MAN", "EUS", rgd_path=feed_paths.rgd,
                            msn_path=msn_path)
    assert dist is not None
    assert dist.method == "rgd_shortest_path"
    assert dist.km == pytest.approx(C2_DIST_REF_KM, rel=C2_DIST_TOL)


@pytest.mark.slow
def test_c2_traction_electric_and_diesel_corridors(feed_paths: FeedPaths) -> None:
    """WCML overwhelmingly electric; Bittern line overwhelmingly diesel.
    WCML reading diesel would be a parser bug, not a data surprise."""
    if feed_paths.timetable_mca is None or not feed_paths.timetable_mca.exists():
        pytest.skip("no timetable .MCA in data/")
    from src.ingest.timetable import load_timetable_index, traction_mix

    idx = load_timetable_index(feed_paths.timetable_mca)
    wcml = traction_mix(idx, "MAN", "EUS")
    assert wcml.train_count > 0
    assert wcml.electric_pct >= C2_TRACTION_MIN_SHARE
    assert wcml.diesel_pct < wcml.electric_pct

    bittern = traction_mix(idx, "NRW", "SHM")
    assert bittern.train_count > 0
    assert bittern.diesel_pct >= C2_TRACTION_MIN_SHARE


@pytest.mark.slow
def test_carbon_block_end_to_end(feed_paths: FeedPaths) -> None:
    """Requesting carbon alone auto-adds demand, produces corridor
    per-passenger figures (what gate C3 checks) and a volume-backed
    total from the ODM fixture, all labelled ESTIMATE."""
    change = ChangeRequest(
        kind="add_railcard", railcard_code="STU", discount_pct=1.0 / 3.0,
        discount_categories=("01",), corridor_origin_nlc="2968",
        corridor_dest_nlc="1444", peak_valid=True,
        description="carbon freeze test")
    report = compute_impact(change, feed_paths, include={"carbon"})

    assert report.demand is not None, "carbon must auto-add demand"
    carbon = report.carbon
    assert carbon is not None
    assert carbon.notes and carbon.notes[0].startswith("ESTIMATE")

    assert carbon.corridor_distance_km is not None
    assert carbon.corridor_saving_kg_per_passenger is not None
    assert carbon.corridor_saving_kg_per_passenger > 0
    assert carbon.corridor_car_kg_per_passenger is not None
    assert carbon.corridor_rail_kg_per_passenger is not None
    assert (carbon.corridor_car_kg_per_passenger
            > carbon.corridor_rail_kg_per_passenger)

    vol_rows = [e for e in carbon.estimates if e.net_new_journeys is not None]
    assert vol_rows, "ODM fixture should give at least one volume-backed row"
    assert carbon.total_carbon_saving_kg is not None
    assert carbon.total_carbon_saving_kg > 0
    for e in vol_rows:
        # Block values are rounded to 0.1 kg for display.
        assert e.carbon_saving_kg == pytest.approx(
            e.net_new_journeys * e.distance_km
            * (e.car_kgco2e_per_pkm - e.rail_kgco2e_per_pkm), abs=0.05)
