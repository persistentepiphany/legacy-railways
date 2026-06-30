"""End-to-end impact engine test: the demo Student-railcard ChangeRequest.

Pass condition for the impact engine: the demo ChangeRequest produces an
ImpactReport with exact, hand-computed values for the canonical count, the
revenue exposure, and the blast-radius count; and the structural checks
(provenance shape, no-flow propagation, inversion detection) all hold.

Marked `@pytest.mark.slow` because compute_impact builds the FFL index
(~250 MB scanned on first call; cached after).

Run with:   pytest tests/test_impact_demo_corridor.py -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact import (
    ChangeRequest,
    FeedPaths,
    ImpactReport,
    compute_impact,
    inject_synthetic_railcard,
)
from src.ingest.inspect import (
    load_frr_rules,
    load_rcm_min_fares,
    load_ticket_discount_categories,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

# NLCs verified against .LOC (see test_regulation_map.py for the same set).
MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return paths


@pytest.fixture(scope="module")
def demo_change() -> ChangeRequest:
    """The headline demo change from docs/HACKATHON.md §3 Showpiece 2:
    'add a Student railcard, 1/3 off, peak-valid on Manchester–London'.
    discount_categories=('01',) is the locked scope (plan: cosmic-twirling-noodle.md
    open question 2)."""
    return ChangeRequest(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Add Student railcard, 1/3 off, peak-valid on MAN->EUS",
    )


@pytest.fixture(scope="module")
def demo_report(feed_paths: FeedPaths, demo_change: ChangeRequest) -> ImpactReport:
    return compute_impact(demo_change, feed_paths)


# --- Smoke + structural shape ---------------------------------------------


@pytest.mark.slow
def test_demo_change_produces_report(demo_report: ImpactReport) -> None:
    """`compute_impact` returns an ImpactReport with the demo change attached
    and at least one canonical row. The simplest possible regression guard."""
    assert demo_report.change.railcard_code == "STU"
    assert len(demo_report.canonical_affected) > 0


# --- Hand-computed exact values (tight assertions) ------------------------


# Hand-computed by walking FFLIndexes for the MAN-EUS cluster cross-product,
# filtering to .TTY DISCOUNT_CATEGORY='01', and applying the synthetic rule
# new = floor_5p(adult - int(adult * 1/3)). 13 distinct (flow_id, ticket)
# rows touched on the current RJFAF805 snapshot.
EXPECTED_CANONICAL_COUNT = 13
EXPECTED_PER_FLOW_EXPOSURE_PENCE = -130_490
EXPECTED_PER_PAIR_EXPOSURE_PENCE = -7_949_410
EXPECTED_BLAST_RADIUS_PAIR_COUNT = 765


@pytest.mark.slow
def test_demo_change_canonical_count(demo_report: ImpactReport) -> None:
    """Hand-computed exact: 13 distinct (flow_id, ticket_code) rows on the
    cluster cross-product (origin in {2968, 0438} × dest in {1444, 1072})
    have .TTY DISCOUNT_CATEGORY='01'. If the feed snapshot changes, this
    assertion loudly fails so we re-snapshot deliberately."""
    assert len(demo_report.canonical_affected) == EXPECTED_CANONICAL_COUNT, (
        f"expected {EXPECTED_CANONICAL_COUNT} canonical rows, got "
        f"{len(demo_report.canonical_affected)}; check the LOC group fan-out "
        f"and the DISCOUNT_CATEGORY filter in src/impact/affected.py"
    )


@pytest.mark.slow
def test_demo_change_per_flow_exposure(demo_report: ImpactReport) -> None:
    """Hand-computed exact: sum of (new - old) over the 13 canonical rows
    using the synthetic-rule arithmetic. Negative because it's a discount."""
    assert demo_report.revenue is not None
    assert demo_report.revenue.per_flow_exposure_pence == EXPECTED_PER_FLOW_EXPOSURE_PENCE


@pytest.mark.slow
def test_demo_change_per_pair_exposure(demo_report: ImpactReport) -> None:
    """Cluster-weighted exposure (per-flow delta × blast-radius count).
    NEVER use as revenue — that's the per_flow number. This guards the
    cluster fan-out arithmetic in compute_affected_set."""
    assert demo_report.revenue is not None
    assert demo_report.revenue.per_pair_exposure_pence == EXPECTED_PER_PAIR_EXPOSURE_PENCE


@pytest.mark.slow
def test_demo_change_blast_radius_count(demo_report: ImpactReport) -> None:
    """Total blast-radius pairs across all canonical rows. Each group-flow
    canonical row contributes (members(0438) × members(1072)); direct-flow
    rows contribute 1. The exact total guards the reverse-LOC lookup."""
    assert len(demo_report.blast_radius_pairs) == EXPECTED_BLAST_RADIUS_PAIR_COUNT


# --- Provenance shape -----------------------------------------------------


@pytest.mark.slow
def test_demo_change_provenance_shape(demo_report: ImpactReport) -> None:
    """Every canonical row carries a non-empty provenance chain whose last
    step is the synthetic-discount application — the marker that says
    'this row was repriced, here's the rule'."""
    assert len(demo_report.canonical_affected) > 0
    for fare in demo_report.canonical_affected:
        assert len(fare.provenance) >= 2, (
            f"canonical row {fare.flow_id}/{fare.ticket_code}: "
            f"provenance too short ({len(fare.provenance)}); expected "
            "[affected_set_pick, synthetic_railcard_apply]"
        )
        steps = [p.step for p in fare.provenance]
        assert steps[-1] == "synthetic_railcard_apply", (
            f"{fare.flow_id}/{fare.ticket_code}: provenance steps {steps}; "
            "last step must be synthetic_railcard_apply"
        )
        assert "RJFAF805.FFL" in fare.provenance[0].source, (
            f"first provenance step must cite RJFAF805.FFL; got "
            f"{fare.provenance[0].source!r}"
        )


@pytest.mark.slow
def test_demo_change_headline_fare_via_injection(feed_paths: FeedPaths, demo_change: ChangeRequest) -> None:
    """The injected path produces a railcard chain structurally identical to
    a real .RLC/.DIS/.RCM/.FRR/.TTY chain — what the rule-trace showpiece
    reads. Sequence of step names must match the resolver's railcard chain
    (proves the demo's headline UI card is honest, not stitched from constants).
    """
    ticket_categories = load_ticket_discount_categories(feed_paths.tty)
    frr_rules = load_frr_rules(feed_paths.frr)
    rcm_min_fares = load_rcm_min_fares(feed_paths.rcm)

    # MAN-EUS SOR direct adult price is 14000 (= £140) per BRFares oracle
    # and the FFL flow 0627906.
    outcome = inject_synthetic_railcard(
        adult_pence=14000,
        change=demo_change,
        ticket_code="SOR",
        ticket_categories=ticket_categories,
        frr_rules=frr_rules,
        rcm_min_fares=rcm_min_fares,
    )
    assert outcome.price_pence is not None, (
        f"injected path quarantined: {outcome.quarantine_reason}"
    )
    # Same chain shape as src/resolver/railcard.py:apply_railcard_from_feed —
    # this is the test that says "the demo card is real, not stitched".
    steps = [p.step for p in outcome.provenance]
    expected = [
        "railcard_lookup",
        "discount_category_lookup",
        "discount_lookup",
        "discount_apply",
        "min_fare_floor",
        "rounding",
    ]
    assert steps == expected, f"injected chain steps {steps} != expected {expected}"
    # The synthetic rows must be marked (synthetic) — the UI can highlight
    # this so a reviewer can see which steps came from the proposal.
    rlc_step = next(p for p in outcome.provenance if p.step == "railcard_lookup")
    assert "synthetic" in rlc_step.source.lower()


# --- Inversion detection --------------------------------------------------


@pytest.mark.slow
def test_demo_change_inversion_flagged(demo_report: ImpactReport) -> None:
    """At least one structural inversion fires on the demo. With the current
    snapshot two `return_cheaper_than_single` inversions fire (SOR after
    discount cheaper than VCJ, an Avanti group ticket). The exact count is
    snapshot-dependent; the >= 1 guard catches both the headline demo beat
    and any regression that silently disables the detector."""
    assert demo_report.anomalies is not None
    assert len(demo_report.anomalies.inversions) >= 1
    rules = {inv.rule for inv in demo_report.anomalies.inversions}
    assert rules, "inversions were detected but no rule fields populated"
    # All detected inversions must carry a non-empty explanation for the UI.
    for inv in demo_report.anomalies.inversions:
        assert inv.explanation.strip()
        assert 0 <= inv.lower_price_pence < inv.higher_price_pence


# --- Failure propagation: no-flow, contradiction --------------------------


@pytest.mark.slow
def test_demo_change_no_flow_returns_empty(feed_paths: FeedPaths) -> None:
    """A ChangeRequest whose discount_categories don't intersect the corridor
    must return an empty canonical set (and a `notes[]` entry explaining the
    no-op), not crash. CLAUDE.md: quarantine, never silently guess."""
    # Cat '09' exists in .TTY (22 tickets globally) but no MAN-EUS ticket uses it.
    change_no_flow = ChangeRequest(
        kind="add_railcard",
        railcard_code="NOO",
        discount_pct=1.0 / 3.0,
        discount_categories=("09",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description="No-op change for failure-mode test",
    )
    report = compute_impact(change_no_flow, feed_paths)
    assert len(report.canonical_affected) == 0
    assert len(report.blast_radius_pairs) == 0
    assert report.revenue is not None
    assert report.revenue.per_flow_exposure_pence == 0
    # The notes list must explain the no-op honestly.
    notes_joined = " | ".join(report.notes)
    assert "no fares matched" in notes_joined or "no-op" in notes_joined.lower()


@pytest.mark.slow
def test_validate_against_feed_rejects_unknown_nlc(feed_paths: FeedPaths) -> None:
    """Boundary check: a ChangeRequest with an NLC that doesn't exist in .LOC
    is rejected by compute_impact via the feed validator. CLAUDE.md: never
    let an LLM construct a bogus change and have us silently produce a
    report against missing entities."""
    change = ChangeRequest(
        kind="add_railcard",
        railcard_code="STX",
        discount_pct=0.25,
        discount_categories=("01",),
        corridor_origin_nlc="9999",          # not in .LOC
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description="Boundary test — bad NLC",
    )
    with pytest.raises(ValueError) as exc:
        compute_impact(change, feed_paths)
    assert "not in .LOC" in str(exc.value)


@pytest.mark.slow
def test_validate_against_feed_rejects_existing_railcard_code(feed_paths: FeedPaths) -> None:
    """Cannot silently shadow a real railcard with a synthetic one."""
    change = ChangeRequest(
        kind="add_railcard",
        railcard_code="YNG",                 # already in .RLC
        discount_pct=0.25,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description="Collision test",
    )
    with pytest.raises(ValueError) as exc:
        compute_impact(change, feed_paths)
    assert "already exists in .RLC" in str(exc.value)


# --- Notes / honest gaps disclosed --------------------------------------


@pytest.mark.slow
def test_demo_change_notes_disclose_known_limitations(demo_report: ImpactReport) -> None:
    """Both the .RCM floor omission and the .RST peak-restriction omission
    must be surfaced in the report's notes list — these are the assumptions
    the UI must echo so a reviewer can challenge them. CLAUDE.md: flag
    rather than fabricate."""
    joined = " | ".join(demo_report.notes)
    assert "RCM" in joined, "expected .RCM disclosure in notes; missing"
    assert "RST" in joined or "peak_valid" in joined, (
        "expected .RST/peak_valid disclosure in notes; missing"
    )


# --- Construction-time validation (fast / no feed) ----------------------


def test_change_request_rejects_bad_discount_pct() -> None:
    """The dataclass enforces 0 < discount_pct < 1 strictly at construction."""
    with pytest.raises(ValueError):
        ChangeRequest(
            kind="add_railcard", railcard_code="STU",
            discount_pct=0.0,                        # invalid
            discount_categories=("01",),
            corridor_origin_nlc="2968", corridor_dest_nlc="1444",
            peak_valid=False, description="bad",
        )
    with pytest.raises(ValueError):
        ChangeRequest(
            kind="add_railcard", railcard_code="STU",
            discount_pct=1.0,                        # invalid
            discount_categories=("01",),
            corridor_origin_nlc="2968", corridor_dest_nlc="1444",
            peak_valid=False, description="bad",
        )


def test_change_request_rejects_bad_railcard_code() -> None:
    with pytest.raises(ValueError):
        ChangeRequest(
            kind="add_railcard", railcard_code="ST",   # too short
            discount_pct=0.25,
            discount_categories=("01",),
            corridor_origin_nlc="2968", corridor_dest_nlc="1444",
            peak_valid=False, description="bad",
        )
