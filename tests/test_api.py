"""FastAPI surface tests — TestClient against src.api.main.app.

Covers the three showpieces' contract:
  1. Resolve returns a ResolvedFare with provenance.
  2. Impact for the demo ChangeRequest returns a report with compliance.
  3. Propose → list → approve → escalation flow, with baseline untouched.

Plus boundary-error discipline: bad input → clean 400, never a 500 stack."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Tests boot the real app against the real data dir; never let them replay,
# append to, or truncate the dev server's staging journal.
os.environ["FARES_STAGING_JOURNAL"] = "off"

from src.api.main import app  # noqa: E402
from src.impact import FeedPaths  # noqa: E402
from src.ingest.inspect import load_ffl_indexes  # noqa: E402


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


# --- 4. Metadata endpoints (snapshot / corridors / stations / railcards) ---


def test_snapshot_returns_feed_id_and_records(client: TestClient) -> None:
    r = client.get("/api/snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "RJFAF805"
    assert body["feed"] == "RJFAF"
    assert body["sequence"] == "805"
    # Records count comes from the .FFL — non-zero regardless of header.
    assert body["records"] > 1_000_000


def test_corridors_endpoint_returns_ten_corridors_with_real_nlcs(
    client: TestClient,
) -> None:
    r = client.get("/api/corridors")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 10
    # Manchester-Euston must be present — it's the CLAUDE.md demo corridor.
    man_eus = [c for c in body if c["id"] == "man-eus"][0]
    assert man_eus["origin_nlc"] == MAN_PICC_NLC
    assert man_eus["dest_nlc"] == EUSTON_NLC
    assert man_eus["origin_crs"] == "MAN"
    assert man_eus["dest_crs"] == "EUS"


def test_stations_endpoint_projects_real_coords(client: TestClient) -> None:
    r = client.get("/api/stations")
    assert r.status_code == 200
    body = r.json()
    # Full MSN load is ~3000+ stations with real coords.
    assert len(body) > 2500
    by_crs = {s["crs"]: s for s in body}
    # North-south sanity: Aberdeen must be visibly above (lower y) Penzance.
    assert by_crs["ABD"]["y"] < by_crs["PNZ"]["y"], (
        "OSGB→SVG projection is upside-down"
    )
    # East-west sanity: Kings Cross must be east of Manchester.
    assert by_crs["KGX"]["x"] > by_crs["MAN"]["x"]
    # Every corridor terminus must have a real NLC join.
    for crs in ("MAN", "EUS", "KGX", "EDB", "PNZ", "CDF"):
        assert by_crs[crs]["nlc"] is not None, f"{crs} missing NLC join"


def test_railcards_endpoint_includes_national_cards_with_feed_flag(
    client: TestClient,
) -> None:
    r = client.get("/api/railcards")
    assert r.status_code == 200
    body = r.json()
    codes = {rc["code"] for rc in body}
    # The four biggest national passenger railcards must be surfaced.
    for expected in ("YNG", "SRN", "2TR", "FAM"):
        assert expected in codes, f"national railcard {expected} not in list"
    # The `in_feed` flag joins against the current .RLC snapshot — YNG is
    # always present in the current live feed.
    yng = [rc for rc in body if rc["code"] == "YNG"][0]
    assert yng["in_feed"] is True


# --- 5. ChangeRequest extensions actually change the answer ---------------


def test_impact_honours_rounding_rule_near10(client: TestClient) -> None:
    body = _demo_change()
    body["rounding_rule"] = "near10"
    r = client.post("/api/impact?include=", json=body)
    assert r.status_code == 200, r.text
    prices = [
        af["new_price_pence"]
        for af in r.json()["canonical_affected"]
        if af["new_price_pence"] is not None
    ]
    assert prices, "expected at least one repriced fare"
    for p in prices:
        assert p % 10 == 0, f"near10 rounding rule left a fare not on a 10p band: {p}"


def test_impact_honours_min_floor_pct(client: TestClient) -> None:
    baseline_body = _demo_change()
    baseline_body["discount_pct"] = 0.5
    floor_body = dict(baseline_body)
    floor_body["min_floor_pct"] = 0.75

    r_baseline = client.post("/api/impact?include=", json=baseline_body)
    r_floor = client.post("/api/impact?include=", json=floor_body)
    assert r_baseline.status_code == 200 and r_floor.status_code == 200

    def _rekey(payload):
        return {
            (af["flow_id"], af["ticket_code"]): af["new_price_pence"]
            for af in payload["canonical_affected"]
            if af["new_price_pence"] is not None
        }

    baseline = _rekey(r_baseline.json())
    floored = _rekey(r_floor.json())
    shared_keys = baseline.keys() & floored.keys()
    assert shared_keys, "both requests should reprice at least one common fare"
    # For at least one shared fare, the floored version must be strictly
    # >= the baseline version — the 75% floor bites on deeper discounts.
    ge_count = sum(1 for k in shared_keys if floored[k] >= baseline[k])
    assert ge_count == len(shared_keys), (
        "min_floor_pct=0.75 should never yield a lower price than the unfloored run"
    )
    strictly_higher = [k for k in shared_keys if floored[k] > baseline[k]]
    assert strictly_higher, "expected the floor to bite on at least one fare"


# --- 6. Contradiction re-propose-with-choice unblocks the second proposal --


def test_contradiction_choice_unblocks_repropose(client: TestClient) -> None:
    client.post("/api/staging/reset")
    first = _demo_change()
    first["description"] = "First proposal · 33% off"
    r1 = client.post("/api/staging/propose", json=first)
    assert r1.status_code == 200 and r1.json()["kind"] == "accepted"

    conflicting = dict(first)
    conflicting["discount_pct"] = 0.20
    conflicting["description"] = "Second proposal · 20% off (conflicts)"
    r2 = client.post("/api/staging/propose", json=conflicting)
    assert r2.status_code == 200
    body2 = r2.json()
    if body2["kind"] != "escalation":
        pytest.skip("second proposal did not conflict on this snapshot")
    conflicts = body2["contradictions"]
    assert conflicts, "escalation must expose at least one contradiction"
    conflicting["contradiction_choice"] = {
        f"{cp['flow_id']}:{cp['ticket_code']}": "B" for cp in conflicts
    }
    r3 = client.post("/api/staging/propose", json=conflicting)
    assert r3.status_code == 200, r3.text
    assert r3.json()["kind"] == "accepted", (
        "re-propose with contradiction_choice for every conflict should succeed"
    )


def test_contradiction_choice_unknown_key_is_400(client: TestClient) -> None:
    """A contradiction_choice key that matches no detected contradiction is
    an error — silently ignoring it would make a stale/typo'd key look like
    a resolved contradiction."""
    client.post("/api/staging/reset")
    first = _demo_change()
    r1 = client.post("/api/staging/propose", json=first)
    assert r1.status_code == 200 and r1.json()["kind"] == "accepted"

    conflicting = dict(first)
    conflicting["discount_pct"] = 0.20
    conflicting["contradiction_choice"] = {"9999999:XXX": "B"}
    r2 = client.post("/api/staging/propose", json=conflicting)
    assert r2.status_code == 400, r2.text
    assert "9999999:XXX" in r2.json()["detail"]


def test_contradiction_choice_a_keeps_escalation(client: TestClient) -> None:
    """Choosing 'A' (keep the existing card) means the proposal as written
    still conflicts — the outcome stays an escalation, never a silent accept."""
    client.post("/api/staging/reset")
    first = _demo_change()
    r1 = client.post("/api/staging/propose", json=first)
    assert r1.status_code == 200 and r1.json()["kind"] == "accepted"

    conflicting = dict(first)
    conflicting["discount_pct"] = 0.20
    r2 = client.post("/api/staging/propose", json=conflicting)
    assert r2.status_code == 200
    body2 = r2.json()
    if body2["kind"] != "escalation":
        pytest.skip("second proposal did not conflict on this snapshot")
    conflicting["contradiction_choice"] = {
        f"{cp['flow_id']}:{cp['ticket_code']}": "A"
        for cp in body2["contradictions"]
    }
    r3 = client.post("/api/staging/propose", json=conflicting)
    assert r3.status_code == 200, r3.text
    assert r3.json()["kind"] == "escalation", (
        "'A' (keep existing) must not accept the conflicting proposal"
    )


# --- 7. Provenance carries the raw fixed-width feed line ------------------


def test_resolve_provenance_includes_raw_record(client: TestClient) -> None:
    r = client.get(
        "/api/resolve",
        params={"origin": MAN_PICC_NLC, "dest": EUSTON_NLC, "ticket": "SOR"},
    )
    assert r.status_code == 200
    steps = r.json()["provenance"]
    raw_steps = [s for s in steps if s.get("raw_record")]
    assert raw_steps, "expected at least one step carrying a raw feed line"
    # The flow_record step should carry a real FFL record (starts with 'RF').
    flow_step = [s for s in steps if s["step"] == "flow_record"][0]
    assert flow_step["raw_record"], "flow_record must carry raw_record"
    assert flow_step["raw_record"].startswith("RF"), (
        f"expected FFL RF-record, got {flow_step['raw_record'][:20]!r}"
    )


# --- 8. Operator (TOC) scope ------------------------------------------------


def test_tocs_endpoint_contract(client: TestClient) -> None:
    """GET /api/tocs lists fare-TOCs with .TOC names. Counts/stations are
    None until the warm thread finishes the .FFL parse (honest partial), so
    only assert them when present."""
    r = client.get("/api/tocs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list) and len(body) > 0
    nth = next((t for t in body if t["code"] == "NTH"), None)
    assert nth is not None, "NTH missing from /api/tocs"
    assert nth["toc_2char"] == "NT"
    assert nth["name"] == "NORTHERN"
    if nth["flow_count"] is not None:
        assert nth["actual_flow_count"] <= nth["flow_count"]
        assert 0 < len(nth["station_nlcs"]) <= 2_500


def _toc_change_body(toc_code: str) -> dict:
    body = _demo_change()
    body.update(
        scope="toc",
        toc_code=toc_code,
        corridor_origin_nlc="",
        corridor_dest_nlc="",
        description=f"Add Student railcard, 1/3 off, all {toc_code} flows",
    )
    return body


def test_impact_toc_scope_with_corridor_nlcs_is_400(client: TestClient) -> None:
    body = _toc_change_body("NTH")
    body["corridor_origin_nlc"] = MAN_PICC_NLC
    body["corridor_dest_nlc"] = EUSTON_NLC
    r = client.post("/api/impact", json=body)
    assert r.status_code == 400
    assert "empty corridor" in r.json()["detail"]


@pytest.mark.slow
def test_impact_unknown_toc_code_is_400(client: TestClient) -> None:
    r = client.post("/api/impact", json=_toc_change_body("ZZZ"), params={"include": ""})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "has no flows in .FFL" in detail
    assert "NTH" in detail  # error lists the known codes


@pytest.mark.slow
def test_impact_toc_scope_returns_bounded_report(client: TestClient) -> None:
    r = client.post(
        "/api/impact",
        json=_toc_change_body("NTH"),
        params={"include": "revenue,compliance"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    st = body["scope_stats"]
    assert st["scope"] == "toc" and st["toc_code"] == "NTH"
    assert st["truncated"] is True
    assert len(body["canonical_affected"]) == st["canonical_returned"] <= 200
    assert st["canonical_total"] > st["canonical_returned"]
    assert body["compliance"]["partial"] is True
