"""End-to-end integration test: compliance + staging + non-mutation + escalation.

This is the load-bearing test that proves the three new pieces meet:

  1. The demo ChangeRequest flows through compute_impact and the returned
     ImpactReport carries a compliance verdict on every canonical row.
  2. The change can be proposed and approved into the staging layer; the
     baseline FFL indexes are fingerprint-identical before and after,
     proving no code path from a proposal to a baseline mutation.
  3. A deliberately-constructed second change targeting the SAME canonical
     row at a different new_price returns an Escalation — the engine
     refuses to auto-resolve, surfaces both options with evidence.

If this test breaks, the demo's "control" verb is broken. Marked @slow
because it runs the full FFL scan via compute_impact."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact import (
    ChangeRequest,
    FeedPaths,
    ImpactReport,
    compute_impact,
)
from src.ingest.inspect import load_ffl_indexes
from src.staging import (
    Accepted,
    Escalation,
    StagingLayer,
    approve,
    propose,
)


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


def _fingerprint_ffl(ffl_path: Path) -> tuple[int, int, int]:
    """Same cheap structural hash as test_staging.py:_fingerprint_ffl.

    Re-defined here rather than imported to keep the two tests
    independent — if one is run alone, no cross-module dependency."""
    idx = load_ffl_indexes(ffl_path)
    return (
        len(idx.flows_by_pair),
        len(idx.fares_by_flow),
        sum(len(fs) for fs in idx.fares_by_flow.values()),
    )


def _rebuild_report_with_perturbed_prices(
    original: ImpactReport,
    delta_pence: int,
    new_description: str,
) -> ImpactReport:
    """Construct a sibling ImpactReport repricing every canonical row by
    `delta_pence` relative to the original. Used to manufacture the
    deliberate-contradiction scenario: same canonical rows, different
    new_price, so the contradiction detector fires on every row."""
    from dataclasses import replace
    perturbed = tuple(
        replace(
            fare,
            new_price_pence=(
                (fare.new_price_pence or 0) + delta_pence
            ),
        )
        for fare in original.canonical_affected
    )
    new_change = replace(original.change, description=new_description)
    return replace(
        original,
        change=new_change,
        canonical_affected=perturbed,
    )


@pytest.mark.slow
def test_demo_flows_end_to_end(feed_paths: FeedPaths) -> None:
    """The integrated artefact: demo change → ImpactReport (with
    compliance flags) → proposed into staging → approved → baseline
    untouched → constructed conflicting change escalates."""
    # --- 1. Demo change → ImpactReport carrying compliance ----------------
    change = ChangeRequest(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Add Student railcard, 1/3 off, peak-valid on MAN->EUS",
    )
    fp_before = _fingerprint_ffl(feed_paths.ffl)

    report = compute_impact(change, feed_paths)

    # Compliance attached on every row.
    assert len(report.canonical_affected) > 0
    for fare in report.canonical_affected:
        assert fare.compliance is not None, (
            f"row {fare.flow_id}/{fare.ticket_code} missing compliance verdict"
        )
        assert fare.compliance.status in ("compliant", "breach", "not_regulated")

    # Discount-only change can't breach on this narrow scope.
    assert report.compliance is not None
    assert report.compliance.breach_count == 0
    # Report carries the regulation-map disclosure for the UI.
    assert any(
        "1 March 2025" in n or "REGULATION.md §4" in n
        for n in report.compliance.regulation_map_notes
    )

    # compute_impact did not mutate the baseline.
    assert _fingerprint_ffl(feed_paths.ffl) == fp_before

    # --- 2. Propose into staging; baseline still untouched --------------
    layer = StagingLayer.empty()
    proposed = propose(layer, change, report)
    assert isinstance(proposed, Accepted)
    assert _fingerprint_ffl(feed_paths.ffl) == fp_before
    # Original layer unchanged (persistent-style).
    assert layer.pending == ()

    # --- 3. Approve; baseline STILL untouched ---------------------------
    approved_outcome = approve(proposed.layer, proposed.card.card_id)
    assert isinstance(approved_outcome, Accepted)
    assert _fingerprint_ffl(feed_paths.ffl) == fp_before
    assert len(approved_outcome.layer.approved) == 1
    assert len(approved_outcome.layer.pending) == 0
    # The approved card carries the SAME report (no copy/rebuild).
    assert approved_outcome.card.impact.canonical_affected is report.canonical_affected

    # --- 4. Constructed conflicting change must escalate -----------------
    # Build a second report that reprices the same canonical rows
    # differently (e.g. a half-off railcard instead of 1/3-off).
    conflicting_change = ChangeRequest(
        kind="add_railcard",
        railcard_code="STX",                       # different code, no collision
        discount_pct=0.5,                          # different discount
        discount_categories=("01",),               # same scope = same canonical rows
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Half-off competing proposal, MAN->EUS",
    )
    conflicting_report = _rebuild_report_with_perturbed_prices(
        report,
        delta_pence=-500,                          # arbitrary non-zero delta
        new_description=conflicting_change.description,
    )
    second = propose(approved_outcome.layer, conflicting_change, conflicting_report)
    assert isinstance(second, Escalation), (
        f"expected Escalation on conflicting proposal; got {type(second).__name__}"
    )
    # Every canonical row from the original now disagrees on price → as
    # many contradictions as canonical rows.
    assert len(second.contradictions) == len(report.canonical_affected)
    assert second.proposed is conflicting_change
    # The escalation refuses to pick — its contradictions list both options.
    for pair in second.contradictions:
        assert pair.option_a["source"] != pair.option_b["source"]
        assert {pair.option_a["source"], pair.option_b["source"]} >= {"proposal"} or (
            "card-" in pair.option_a["source"] or "card-" in pair.option_b["source"]
        )

    # Baseline STILL untouched after the escalation.
    assert _fingerprint_ffl(feed_paths.ffl) == fp_before


@pytest.mark.slow
def test_breach_in_staging_does_not_block_approval(feed_paths: FeedPaths) -> None:
    """The compliance flag is informational, not enforcement: a card with
    a breach can still be proposed and approved (the analyst sees the red
    flag and decides). This matches HACKATHON.md §3: 'the analyst approves
    card-by-card ... the AI proposes, the human disposes.'

    Uses the broad-scope change that organically produces breaches (via
    the §4 cheapest-cap fallback on SVR routes with price variance)."""
    change = ChangeRequest(
        kind="add_railcard",
        railcard_code="STY",
        discount_pct=1.0 / 3.0,
        discount_categories=("01", "03", "05", "08"),  # includes SVR
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Broad-scope Student railcard, MAN->EUS",
    )
    report = compute_impact(change, feed_paths)
    assert report.compliance is not None
    if report.compliance.breach_count == 0:
        pytest.skip("no organic breaches on this snapshot (snapshot drift)")

    # The breach surfaces — that's the UI red flag.
    assert report.compliance.breach_count >= 1
    # But approval still works (no enforcement gate at the engine layer).
    proposed = propose(StagingLayer.empty(), change, report)
    assert isinstance(proposed, Accepted)
    approved_outcome = approve(proposed.layer, proposed.card.card_id)
    assert isinstance(approved_outcome, Accepted)
    # The approved card carries the breach evidence for audit.
    approved_card = approved_outcome.layer.approved[0]
    assert approved_card.impact.compliance is not None
    assert approved_card.impact.compliance.breach_count >= 1
    breach_rows = approved_card.impact.compliance.breaches
    assert len(breach_rows) >= 1
    for f in breach_rows:
        assert f.compliance is not None
        assert f.compliance.status == "breach"
        assert "BREACH" in f.compliance.explanation
