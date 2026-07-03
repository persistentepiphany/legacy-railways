"""GET /api/corridor/stats — route fact sheet against the real feed.

Every figure must carry a basis in `notes`; missing sources degrade to
None + a note, never a 500."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["FARES_STAGING_JOURNAL"] = "off"

from src.api.main import app  # noqa: E402
from src.impact import FeedPaths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return paths


@pytest.fixture()
def client(feed_paths: FeedPaths):  # noqa: ARG001 — fixture order
    with TestClient(app) as c:
        yield c


def test_man_eus_fact_sheet(client: TestClient, feed_paths: FeedPaths) -> None:
    r = client.get("/api/corridor/stats?origin=MAN&dest=EUS")
    assert r.status_code == 200
    j = r.json()
    assert j["origin_crs"] == "MAN" and j["dest_crs"] == "EUS"
    assert j["notes"], "every figure needs a disclosed basis"

    mca = feed_paths.timetable_mca
    if mca is not None and mca.exists():
        assert j["train_count"] and j["train_count"] > 0
        assert j["timetable_source"]
        assert any("ever-calls semantics" in n for n in j["notes"])
    else:
        assert j["train_count"] is None

    if feed_paths.odm_csv is not None and feed_paths.odm_csv.exists():
        # MAN↔EUS is a headline pair — must be present in any real ODM.
        assert j["odm_journeys_out"] and j["odm_journeys_out"] > 0
        assert j["odm_journeys_back"] and j["odm_journeys_back"] > 0
        assert j["odm_period_label"]

    if j["distance_km"] is not None:
        assert j["distance_km"] > 100  # MAN–EUS is ~300 km by rail
        assert j["rail_kgco2e_per_journey"] is not None
        assert j["car_kgco2e_per_journey"] is not None
        assert (j["carbon_saving_per_journey_kg"]
                == round(j["car_kgco2e_per_journey"]
                         - j["rail_kgco2e_per_journey"], 2))


def test_same_station_degrades(client: TestClient) -> None:
    r = client.get("/api/corridor/stats?origin=MAN&dest=MAN")
    assert r.status_code == 200
    j = r.json()
    assert j["train_count"] is None
    assert any("same station" in n for n in j["notes"])


def test_unknown_crs_degrades_not_500(client: TestClient) -> None:
    r = client.get("/api/corridor/stats?origin=ZZZ&dest=EUS")
    assert r.status_code == 200
    j = r.json()
    # No fares NLC for ZZZ → ODM unavailable, disclosed in notes.
    assert j["odm_journeys_out"] is None
    assert j["notes"]


def test_bad_crs_length_is_422(client: TestClient) -> None:
    assert client.get("/api/corridor/stats?origin=MANC&dest=EUS").status_code == 422
