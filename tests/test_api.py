"""FastAPI surface tests — TestClient against src.api.main.app.

Covers the three showpieces' contract:
  1. Resolve returns a ResolvedFare with provenance.
  2. Impact for the demo ChangeRequest returns a report with compliance.
  3. Propose → list → approve → escalation flow, with baseline untouched.

Plus boundary-error discipline: bad input → clean 400, never a 500 stack."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.impact import FeedPaths
from src.ingest.inspect import load_ffl_indexes


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
    # Use context-manager form so lifespan runs (mounts feed_paths +
    # staging on app.state). Each test gets a fresh staging layer because
    # the lifespan re-initialises it.
    with TestClient(app) as c:
        yield c


def _fingerprint_ffl(ffl_path: Path) -> tuple[int, int, int]:
    idx = load_ffl_indexes(ffl_path)
    return (
        len(idx.flows_by_pair),
        len(idx.fares_by_flow),
        sum(len(fs) for fs in idx.fares_by_flow.values()),
    )


def _demo_change() -> dict:
    return {
        "kind": "add_railcard",
        "railcard_code": "STU",
        "discount_pct": 1.0 / 3.0,
        "discount_categories": ["01"],
        "corridor_origin_nlc": MAN_PICC_NLC,
        "corridor_dest_nlc": EUSTON_NLC,
        "peak_valid": True,
        "description": "Add Student railcard, 1/3 off, peak-valid on MAN->EUS",
    }


# --- 1. Resolve ------------------------------------------------------------


@pytest.mark.slow
def test_resolve_demo_corridor_returns_provenance(client: TestClient) -> None:
    r = client.get(
        "/api/resolve",
        params={"origin": MAN_PICC_NLC, "dest": EUSTON_NLC, "ticket": "SOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin_nlc"] == MAN_PICC_NLC
    assert body["dest_nlc"] == EUSTON_NLC
    assert body["ticket_code"] == "SOR"
    assert body["status"] in {
        "resolved", "no_flow", "no_fare", "ambiguous", "suppressed", "contradiction",
    }
    assert isinstance(body["provenance"], list)
    assert len(body["provenance"]) > 0
    for step in body["provenance"]:
        assert set(step) >= {"step", "source", "detail"}


def test_resolve_bad_input_returns_clean_400(client: TestClient) -> None:
    # 4-char NLC required; "XX" fails FastAPI's Query length check → 422.
    r = client.get(
        "/api/resolve",
        params={"origin": "XX", "dest": EUSTON_NLC, "ticket": "SOR"},
    )
    assert r.status_code in (400, 422)
    body = r.json()
    assert "detail" in body  # not an HTML stack trace


def test_resolve_non_alnum_returns_400(client: TestClient) -> None:
    # 4 chars but engine-level _validate_inputs rejects (alnum check).
    r = client.get(
        "/api/resolve",
        params={"origin": "!!!!", "dest": EUSTON_NLC, "ticket": "SOR"},
    )
    assert r.status_code == 400
    assert "alnum" in r.json()["detail"]


# --- 2. Impact -------------------------------------------------------------


@pytest.mark.slow
def test_impact_demo_change_returns_compliance(client: TestClient) -> None:
    r = client.post("/api/impact", json=_demo_change())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["canonical_affected"]) > 0
    for fare in body["canonical_affected"]:
        assert fare["compliance"] is not None
        assert fare["compliance"]["status"] in {
            "compliant", "breach", "not_regulated",
        }
    compliance = body["compliance"]
    assert compliance is not None
    assert any(
        "1 March 2025" in n or "REGULATION.md §4" in n
        for n in compliance["regulation_map_notes"]
    )
    # No breach on the narrow discount-only scope.
    assert compliance["breach_count"] == 0
    # Default include set has compliance + anomalies + revenue, no splits.
    assert body["anomalies"] is not None
    assert body["revenue"] is not None
    assert body["splits"] is None


def test_impact_bad_discount_returns_400(client: TestClient) -> None:
    bad = _demo_change()
    bad["discount_pct"] = 2.0
    r = client.post("/api/impact", json=bad)
    assert r.status_code == 400
    assert "discount_pct" in r.json()["detail"]


# --- 3. Staging: propose → list → approve → escalation ---------------------


@pytest.mark.slow
def test_staging_propose_approve_baseline_untouched(
    client: TestClient, feed_paths: FeedPaths,
) -> None:
    client.post("/api/staging/reset")
    fp_before = _fingerprint_ffl(feed_paths.ffl)

    # Propose
    r = client.post("/api/staging/propose", json=_demo_change())
    assert r.status_code == 200, r.text
    proposed = r.json()
    assert proposed["kind"] == "accepted"
    card_id = proposed["card"]["card_id"]
    assert card_id == "card-0"

    # List shows it in pending
    r = client.get("/api/staging")
    layer = r.json()
    assert [c["card_id"] for c in layer["pending"]] == [card_id]
    assert layer["approved"] == []

    # Approve
    r = client.post(f"/api/staging/{card_id}/approve")
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["kind"] == "accepted"
    assert approved["card"]["status"] == "approved"

    # List shows it in approved
    r = client.get("/api/staging")
    layer = r.json()
    assert layer["pending"] == []
    assert [c["card_id"] for c in layer["approved"]] == [card_id]

    # Baseline FFL untouched throughout.
    assert _fingerprint_ffl(feed_paths.ffl) == fp_before


@pytest.mark.slow
def test_staging_escalation_on_conflicting_proposal(client: TestClient) -> None:
    client.post("/api/staging/reset")
    # Propose + approve the demo.
    r = client.post("/api/staging/propose", json=_demo_change())
    assert r.json()["kind"] == "accepted"
    card_id = r.json()["card"]["card_id"]
    r = client.post(f"/api/staging/{card_id}/approve")
    assert r.json()["kind"] == "accepted"

    # Second change targeting the same canonical rows at a different
    # new_price → engine returns Escalation. STX/0.5 vs STU/0.333 on the
    # same discount_categories scope means every canonical row disagrees.
    conflicting = _demo_change()
    conflicting["railcard_code"] = "STX"
    conflicting["discount_pct"] = 0.5
    conflicting["description"] = "Half-off competing proposal, MAN->EUS"

    r = client.post("/api/staging/propose", json=conflicting)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "escalation"
    assert len(body["contradictions"]) > 0
    for pair in body["contradictions"]:
        assert pair["option_a"]["source"] != pair["option_b"]["source"]


def test_staging_approve_unknown_card_returns_404(client: TestClient) -> None:
    client.post("/api/staging/reset")
    r = client.post("/api/staging/card-bogus/approve")
    assert r.status_code == 404
    assert "card-bogus" in r.json()["detail"]


def test_staging_get_unknown_card_returns_404(client: TestClient) -> None:
    client.post("/api/staging/reset")
    r = client.get("/api/staging/card-bogus")
    assert r.status_code == 404
