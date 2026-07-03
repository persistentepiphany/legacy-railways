"""GET /api/overview — network master view.

The baseline (pricing + inversion scan + volumes) is computed once on the
warm thread; the endpoint must answer ready=false while that runs and
overlay live staging counts once ready."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["FARES_STAGING_JOURNAL"] = "off"

from src.api.main import app  # noqa: E402
from src.impact import FeedPaths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"


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


def _wait_ready(client: TestClient, timeout_s: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        j = client.get("/api/overview").json()
        if j["ready"]:
            return j
        time.sleep(1.0)
    pytest.fail("overview baseline never became ready")


def test_overview_ready_shape(client: TestClient) -> None:
    j = _wait_ready(client)
    assert j["computed_at"]
    assert j["corridors"], "curated corridors must be present"
    for c in j["corridors"]:
        assert c["origin_nlc"] and c["dest_nlc"]
        assert c["aberration_count"] == len(c["aberrations"])
        for ab in c["aberrations"]:
            assert ab["rule"]
            # An inversion is by definition higher-tier priced above lower.
            assert ab["higher_price_pence"] > 0
        assert c["pending_changes"] == 0
        assert c["approved_changes"] == 0


def test_overview_has_demo_corridor_with_fares(client: TestClient) -> None:
    j = _wait_ready(client)
    demo = [c for c in j["corridors"]
            if {c["origin_nlc"], c["dest_nlc"]} == {MAN_PICC_NLC, EUSTON_NLC}]
    assert demo, "MAN↔EUS must be in the curated overview"
    c = demo[0]
    assert c["key_fares"], "demo corridor must resolve headline fares"
    for kf in c["key_fares"]:
        assert kf["price_pence"] > 0


def test_staging_counts_overlay_live(client: TestClient) -> None:
    j = _wait_ready(client)
    demo = [c for c in j["corridors"]
            if {c["origin_nlc"], c["dest_nlc"]} == {MAN_PICC_NLC, EUSTON_NLC}]
    assert demo
    before = demo[0]["pending_changes"]

    r = client.post("/api/staging/propose", json={
        "kind": "add_railcard",
        "railcard_code": "STU",
        "discount_pct": 1.0 / 3.0,
        "discount_categories": ["01"],
        "corridor_origin_nlc": MAN_PICC_NLC,
        "corridor_dest_nlc": EUSTON_NLC,
        "peak_valid": True,
        "description": "Add Student railcard, 1/3 off, peak-valid on MAN->EUS",
    })
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "accepted"

    j2 = client.get("/api/overview").json()
    demo2 = [c for c in j2["corridors"]
             if {c["origin_nlc"], c["dest_nlc"]} == {MAN_PICC_NLC, EUSTON_NLC}]
    assert demo2[0]["pending_changes"] == before + 1
