"""Smoke tests for the split-ticket / fare-arbitrage module.

Lightweight by design — proves the module runs on real feed data, detects a
known opportunity on the demo corridor, and plugs into the modular ImpactReport
contract behind `include="splits"`. A full validation battery (vs TrainSplit
and friends) is a separate session.

Marked `@pytest.mark.slow` because they hit the real RJFAF805 feed via the
resolver, same as `test_impact_demo_corridor.py`.

Run with:   pytest tests/test_splits_demo_corridor.py -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact import (
    ChangeRequest,
    FeedPaths,
    SplitOpportunityResult,
    compute_impact,
    detect_splits,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

# Same NLCs used by test_impact_demo_corridor.py — verified against .LOC.
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
def baseline_splits(feed_paths: FeedPaths) -> SplitOpportunityResult:
    """Module-scoped: SOS = Standard Off-Peak Single, the common walk-up
    ticket on the demo corridor and the default the change-path code uses."""
    return detect_splits(MAN_PICC_NLC, EUSTON_NLC, "SOS", feed_paths)


# --- 1. Module runs on the real feed --------------------------------------


@pytest.mark.slow
def test_detect_splits_runs_on_demo_corridor(
    baseline_splits: SplitOpportunityResult,
    feed_paths: FeedPaths,
) -> None:
    """The module loads, the resolver wires correctly through three legs per
    candidate, and the honest NRCoT Cond. 14 disclosure ships in every result.
    No snapshot-pinned numbers — purely a "does it run and emit shape"
    regression guard."""
    assert isinstance(baseline_splits, SplitOpportunityResult)
    assert baseline_splits.corridor_origin_nlc == MAN_PICC_NLC
    assert baseline_splits.corridor_dest_nlc == EUSTON_NLC
    assert baseline_splits.ticket_code == "SOS"
    assert len(baseline_splits.pre_change) > 0, (
        "expected at least one candidate; check DEMO_CORRIDOR_INTERMEDIATES "
        "vs .LOC membership in src/impact/splits.py"
    )
    # An NRCoT Cond. 14 disclosure must always ship as notes[0] — the
    # canonical slot — in exactly one form: deferred (no timetable) or
    # verified (timetable wired). CLAUDE.md: flag rather than fabricate.
    assert baseline_splits.notes, "result must always ship at least one note"
    canonical = baseline_splits.notes[0]
    assert "NRCoT Cond. 14" in canonical, (
        f"first note must be the NRCoT Cond. 14 disclosure; got {canonical!r}"
    )
    has_deferred = canonical.startswith("Split validity NOT verified")
    has_verified = canonical.startswith("Split validity call-pattern-verified")
    # When the timetable .MCA is present, the verified variant must fire.
    # When absent, the deferred variant must fire. This is the contract
    # that makes the UI safe to render either way.
    tt_present = (
        feed_paths.timetable_mca is not None
        and feed_paths.timetable_mca.exists()
    )
    if tt_present:
        assert has_verified, (
            "timetable .MCA present but result claims validity deferred; "
            "wiring in splits._intermediates_from_timetable likely broken"
        )
    else:
        assert has_deferred, (
            "timetable .MCA absent but result claims call-pattern verified; "
            "fallback path in detect_splits is wrong"
        )
    # Every candidate carries a non-empty provenance chain + explanation
    # string for the UI to render.
    for c in baseline_splits.pre_change:
        assert c.provenance, (
            f"candidate {c.intermediate_nlc}/{c.ticket_code}: empty provenance"
        )
        assert c.explanation.strip(), (
            f"candidate {c.intermediate_nlc}/{c.ticket_code}: empty explanation"
        )
        assert c.status in {"opportunity", "no_saving", "unresolvable"}


# --- 2. Detects a known split on the demo corridor ------------------------


@pytest.mark.slow
def test_detect_splits_finds_known_opportunity(
    baseline_splits: SplitOpportunityResult,
) -> None:
    """At least one WCML candidate (Crewe / Stoke / Stafford / Rugby / MK)
    must surface as an `opportunity` with `saving_pence > 0` on MAN->EUS SOS.
    These are textbook split points; if none fire, either the snapshot moved
    or the through-vs-sum comparison logic broke. On failure, dump every
    candidate's status + saving so the diagnostic is one read away."""
    opportunities = [
        c for c in baseline_splits.pre_change if c.status == "opportunity"
    ]
    if not opportunities:
        dump = "\n".join(
            f"  {c.intermediate_nlc}: status={c.status} "
            f"through={c.through_price_pence} leg1={c.leg1_price_pence} "
            f"leg2={c.leg2_price_pence} saving={c.saving_pence}"
            for c in baseline_splits.pre_change
        )
        pytest.fail(
            "expected at least one split opportunity on MAN->EUS SOS; got "
            f"none. Candidates:\n{dump}"
        )
    # Every flagged opportunity must have a positive saving (sanity guard on
    # the status<->saving invariant in _candidate()).
    for c in opportunities:
        assert c.saving_pence > 0, (
            f"candidate {c.intermediate_nlc} flagged opportunity but "
            f"saving_pence={c.saving_pence}; status<->saving invariant broken"
        )
        assert c.split_total_pence is not None
        assert c.through_price_pence is not None
        assert c.through_price_pence - c.split_total_pence == c.saving_pence


# --- 3. Modular ImpactReport contract: include="splits" gates the block ----


@pytest.mark.slow
def test_compute_impact_with_splits_include_emits_block_only(
    feed_paths: FeedPaths,
) -> None:
    """The locked contract: `include={"splits"}` populates the `splits` block
    on the ImpactReport AND leaves compliance/anomalies/revenue as None. This
    is the first end-to-end demonstration that the modular contract works —
    every future plugin will rely on the same gate behaviour."""
    change = ChangeRequest(
        kind="add_railcard",
        railcard_code="STU",
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=True,
        description="Splits-only demo change",
    )
    report = compute_impact(change, feed_paths, include={"splits"})
    assert report.splits is not None, (
        "splits block missing despite include={'splits'}"
    )
    assert report.compliance is None, (
        "compliance block populated despite not requested"
    )
    assert report.anomalies is None, (
        "anomalies block populated despite not requested"
    )
    assert report.revenue is None, (
        "revenue block populated despite not requested"
    )
    # The change-path result still ships the NRCoT deferral note.
    splits_notes = " | ".join(report.splits.notes)
    assert "NRCoT Cond. 14" in splits_notes
    # pre_change is the populated half; post_change always mirrors it shape-wise.
    assert len(report.splits.pre_change) > 0
    assert len(report.splits.post_change) == len(report.splits.pre_change)
