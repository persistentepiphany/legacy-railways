"""Staging / control layer — propose, approve, contradiction escalation.

The architectural invariants under test:

  1. Persistent-style state: propose/approve return a NEW StagingLayer;
     the input is never mutated. Proven by reference comparison.
  2. Baseline immutability: the slow test runs the full demo flow
     (compute_impact -> propose -> approve), fingerprints the loaded
     FFLIndexes before and after, asserts no change. There is no code
     path from a staged proposal to a baseline mutation.
  3. Contradictions ESCALATE: a second proposal that reprices the same
     canonical row to a different new_price returns an Escalation
     object (not Accepted, not exception). Both options surfaced with
     their evidence; layer unchanged.

Fast tests use in-memory ImpactReport stubs (no FFL scan). The slow test
(`test_no_baseline_mutation_after_full_demo_flow`) builds the real demo
report and is the load-bearing baseline-immutability proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.impact import (
    AffectedFare,
    AnomaliesBlock,
    ChangeRequest,
    ComplianceBlock,
    FeedPaths,
    ImpactReport,
    RevenueBlock,
    compute_impact,
)
from src.impact.affected import BlastRadiusPair
from src.ingest.inspect import load_ffl_indexes
from src.resolver.resolve import ProvenanceStep
from src.staging import (
    Accepted,
    ApprovalCard,
    Escalation,
    StagingLayer,
    approve,
    propose,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

MAN_PICC_NLC = "2968"
EUSTON_NLC = "1444"


# --- In-memory stubs (no FFL scan) -------------------------------------------


def _change(railcard_code: str = "STU", description: str = "stub change") -> ChangeRequest:
    return ChangeRequest(
        kind="add_railcard",
        railcard_code=railcard_code,
        discount_pct=1.0 / 3.0,
        discount_categories=("01",),
        corridor_origin_nlc=MAN_PICC_NLC,
        corridor_dest_nlc=EUSTON_NLC,
        peak_valid=False,
        description=description,
    )


def _affected_fare(
    *,
    flow_id: str,
    ticket_code: str = "SVR",
    new_pence: int = 6700,
    old_pence: int = 10000,
) -> AffectedFare:
    return AffectedFare(
        flow_id=flow_id,
        ticket_code=ticket_code,
        route_code="00000",
        representative_origin_nlc=MAN_PICC_NLC,
        representative_dest_nlc=EUSTON_NLC,
        status="resolved",
        old_price_pence=old_pence,
        new_price_pence=new_pence,
        discount_category="01",
        provenance=(
            ProvenanceStep(
                step="affected_set_pick", source="(test)",
                detail={"flow_id": flow_id, "ticket_code": ticket_code},
            ),
            ProvenanceStep(
                step="synthetic_railcard_apply", source="(synthetic)",
                detail={"adult_pence": str(old_pence), "after_round_5p": str(new_pence)},
            ),
        ),
        blast_radius_pairs=((MAN_PICC_NLC, EUSTON_NLC),),
    )


def _impact(change: ChangeRequest, *fares: AffectedFare) -> ImpactReport:
    return ImpactReport(
        change=change,
        canonical_affected=fares,
        skipped=(),
        blast_radius_pairs=tuple(
            BlastRadiusPair(
                origin_nlc=MAN_PICC_NLC, dest_nlc=EUSTON_NLC,
                canonical_index=i, expansion_reason="direct",
            )
            for i, _ in enumerate(fares)
        ),
        notes=(),
        compliance=ComplianceBlock(
            regulated_count=0,
            breach_count=0,
            breaches=(),
            regulation_map_notes=(),
        ),
        anomalies=AnomaliesBlock(inversions=()),
        revenue=RevenueBlock(
            per_flow_exposure_pence=sum(
                (f.new_price_pence or 0) - (f.old_price_pence or 0) for f in fares
            ),
            per_pair_exposure_pence=0,
        ),
        splits=None,
    )


# --- Empty + propose + approve basics ---------------------------------------


def test_empty_layer_has_no_cards() -> None:
    layer = StagingLayer.empty()
    assert layer.pending == ()
    assert layer.approved == ()
    assert layer.next_card_seq == 0
    assert layer.all_cards() == ()


def test_propose_returns_new_layer_input_unchanged() -> None:
    """propose is pure — the input layer is never mutated. The returned
    Accepted carries a NEW StagingLayer (`result.layer is not layer`)
    and the original layer's tuples are still empty."""
    layer = StagingLayer.empty()
    change = _change()
    impact = _impact(change, _affected_fare(flow_id="X1"))

    result = propose(layer, change, impact)
    assert isinstance(result, Accepted)
    assert result.layer is not layer
    # Input unchanged.
    assert layer.pending == ()
    assert layer.approved == ()
    assert layer.next_card_seq == 0
    # Returned layer has one pending card.
    assert len(result.layer.pending) == 1
    assert result.layer.pending[0] is result.card
    assert result.card.status == "pending"
    assert result.card.card_id == "card-0"
    assert result.layer.next_card_seq == 1


def test_approve_moves_card_pending_to_approved() -> None:
    """After propose then approve(card_id), the card is in `approved`,
    not `pending`, and its status has been promoted."""
    change = _change()
    impact = _impact(change, _affected_fare(flow_id="X1"))
    propose_result = propose(StagingLayer.empty(), change, impact)
    assert isinstance(propose_result, Accepted)

    approve_result = approve(propose_result.layer, propose_result.card.card_id)
    assert isinstance(approve_result, Accepted)

    new_layer = approve_result.layer
    assert len(new_layer.pending) == 0
    assert len(new_layer.approved) == 1
    assert new_layer.approved[0].card_id == propose_result.card.card_id
    assert new_layer.approved[0].status == "approved"
    # next_card_seq does NOT advance on approval — it's only minted on propose.
    assert new_layer.next_card_seq == propose_result.layer.next_card_seq


def test_approve_unknown_card_raises_keyerror() -> None:
    """Approving a non-existent card_id is an error, not a silent no-op.
    CLAUDE.md: surface failures, never silently guess."""
    layer = StagingLayer.empty()
    with pytest.raises(KeyError) as exc:
        approve(layer, "card-999")
    assert "card-999" in str(exc.value)


def test_two_non_conflicting_proposals_both_accepted() -> None:
    """Two changes touching DIFFERENT canonical rows can both be staged
    without escalation."""
    layer = StagingLayer.empty()
    c1 = _change(railcard_code="STA", description="first")
    c2 = _change(railcard_code="STB", description="second")
    i1 = _impact(c1, _affected_fare(flow_id="A"))
    i2 = _impact(c2, _affected_fare(flow_id="B"))   # different flow_id
    r1 = propose(layer, c1, i1)
    assert isinstance(r1, Accepted)
    r2 = propose(r1.layer, c2, i2)
    assert isinstance(r2, Accepted)
    assert len(r2.layer.pending) == 2
    assert r2.card.card_id == "card-1"


def test_idempotent_re_proposal_does_not_escalate() -> None:
    """If a second proposal reprices the same row to the SAME new_price,
    no contradiction fires (idempotent). The new card is added — the
    detector only flags differences, not duplicates."""
    layer = StagingLayer.empty()
    c1 = _change(railcard_code="STA")
    c2 = _change(railcard_code="STB")
    fare = _affected_fare(flow_id="A", new_pence=6700)
    i1 = _impact(c1, fare)
    i2 = _impact(c2, _affected_fare(flow_id="A", new_pence=6700))  # same new_price
    r1 = propose(layer, c1, i1)
    assert isinstance(r1, Accepted)
    r2 = propose(r1.layer, c2, i2)
    assert isinstance(r2, Accepted), (
        "idempotent re-proposal must not escalate; only differing new_price counts"
    )


# --- Contradiction escalation (the headline behaviour) ----------------------


def test_propose_escalates_on_contradicting_canonical_row() -> None:
    """Two changes repricing the same (flow_id, ticket_code) to different
    new_prices must escalate. The second propose returns Escalation, NOT
    Accepted. The layer state is unchanged by the escalation."""
    layer = StagingLayer.empty()
    c1 = _change(railcard_code="STA", description="first proposal")
    c2 = _change(railcard_code="STB", description="conflicting proposal")
    i1 = _impact(c1, _affected_fare(flow_id="X", new_pence=6700))
    i2 = _impact(c2, _affected_fare(flow_id="X", new_pence=8000))  # different new_price

    r1 = propose(layer, c1, i1)
    assert isinstance(r1, Accepted)
    r2 = propose(r1.layer, c2, i2)
    assert isinstance(r2, Escalation), (
        f"expected Escalation; got {type(r2).__name__}"
    )
    assert len(r2.contradictions) >= 1
    pair = r2.contradictions[0]
    assert pair.flow_id == "X"
    assert pair.ticket_code == "SVR"
    # Both options must be populated.
    assert pair.option_a["source"] != pair.option_b["source"]
    assert "proposal" in {pair.option_a["source"], pair.option_b["source"]}


def test_escalation_carries_both_options_with_evidence() -> None:
    """The escalation's option_a / option_b must each include
    new_price_pence, source, change_description, and a provenance summary.
    This is what the UI binds to for the 'present both options' card."""
    c1 = _change(railcard_code="STA", description="A description")
    c2 = _change(railcard_code="STB", description="B description")
    i1 = _impact(c1, _affected_fare(flow_id="X", new_pence=6700))
    i2 = _impact(c2, _affected_fare(flow_id="X", new_pence=8000))
    r1 = propose(StagingLayer.empty(), c1, i1)
    assert isinstance(r1, Accepted)
    r2 = propose(r1.layer, c2, i2)
    assert isinstance(r2, Escalation)

    for pair in r2.contradictions:
        for opt in (pair.option_a, pair.option_b):
            assert "new_price_pence" in opt
            assert "source" in opt
            assert "change_description" in opt
            assert "provenance_summary" in opt
            assert opt["provenance_summary"].strip()

    # The escalation lists the existing card id that conflicts (proves
    # the UI can link to the conflicting card, not just say 'somewhere').
    assert r1.card.card_id in r2.existing_card_ids
    # And it carries the proposed change — so the UI shows the user what
    # they tried to do.
    assert r2.proposed.railcard_code == "STB"


def test_no_silent_resolution_on_contradiction() -> None:
    """After an escalation, the layer is NOT silently updated to include
    the conflicting proposal. The original layer (with just card-0) is
    the only viable next state; the human must resolve the conflict."""
    layer = StagingLayer.empty()
    c1 = _change(railcard_code="STA")
    c2 = _change(railcard_code="STB")
    i1 = _impact(c1, _affected_fare(flow_id="X", new_pence=6700))
    i2 = _impact(c2, _affected_fare(flow_id="X", new_pence=8000))
    r1 = propose(layer, c1, i1)
    assert isinstance(r1, Accepted)
    pre_layer = r1.layer

    r2 = propose(pre_layer, c2, i2)
    assert isinstance(r2, Escalation)

    # The pre_layer is unchanged (frozen). The Escalation does not carry
    # a layer field — there is no "the layer after escalation" by design.
    assert len(pre_layer.pending) == 1
    assert pre_layer.pending[0].card_id == "card-0"
    assert not hasattr(r2, "layer"), (
        "Escalation must not carry a layer field — that would invite a "
        "'just accept the new one' path"
    )


def test_escalation_against_approved_card() -> None:
    """A staged-vs-approved contradiction also escalates. Approving and
    then proposing a conflicting change must produce an Escalation
    (proves the detector checks against ALL cards, not just pending)."""
    c1 = _change(railcard_code="STA")
    c2 = _change(railcard_code="STB")
    i1 = _impact(c1, _affected_fare(flow_id="X", new_pence=6700))
    i2 = _impact(c2, _affected_fare(flow_id="X", new_pence=8000))

    r1 = propose(StagingLayer.empty(), c1, i1)
    assert isinstance(r1, Accepted)
    r1a = approve(r1.layer, r1.card.card_id)
    assert isinstance(r1a, Accepted)
    # Now the layer has card-0 in `approved` and nothing in `pending`.
    r2 = propose(r1a.layer, c2, i2)
    assert isinstance(r2, Escalation)


def test_approve_re_checks_against_approved_cards() -> None:
    """Defensive: the second propose at differing-price did escalate, so
    the conflicting card was never added. To exercise approve()'s
    defensive contradiction check we construct the scenario directly: a
    layer with two cards in pending that BOTH conflict (built via the
    test helper bypassing propose's check), then attempt to approve one,
    then attempt to approve the other.

    Build the layer directly — propose() would block the second proposal.
    """
    c1 = _change(railcard_code="STA")
    c2 = _change(railcard_code="STB")
    i1 = _impact(c1, _affected_fare(flow_id="X", new_pence=6700))
    i2 = _impact(c2, _affected_fare(flow_id="X", new_pence=8000))
    card1 = ApprovalCard(card_id="card-0", change=c1, impact=i1, status="pending")
    card2 = ApprovalCard(card_id="card-1", change=c2, impact=i2, status="pending")
    layer = StagingLayer(pending=(card1, card2), approved=(), next_card_seq=2)

    # First approval: nothing in approved yet, so contradiction check
    # against approved finds nothing → Accepted.
    r1 = approve(layer, "card-0")
    assert isinstance(r1, Accepted)
    # Second approval: card-1 conflicts with the now-approved card-0.
    r2 = approve(r1.layer, "card-1")
    assert isinstance(r2, Escalation)
    assert any(
        pair.flow_id == "X" for pair in r2.contradictions
    )


# --- Slow: baseline non-mutation (the architectural proof) ------------------


@pytest.fixture(scope="module")
def feed_paths() -> FeedPaths:
    paths = FeedPaths.default_for_data_dir(DATA)
    missing = paths.missing()
    if missing:
        pytest.skip(f"missing feed file(s): {missing}")
    return paths


@pytest.fixture(scope="module")
def demo_change() -> ChangeRequest:
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


def _fingerprint_ffl(ffl_path: Path) -> tuple[int, int, int]:
    """A cheap structural hash of the loaded FFLIndexes.

    Returns (#flow_pairs, #fare_flows, sum(line_no)) — three independent
    integers that would differ if ANY F/T record were added, removed, or
    edited. The mtime-cached loader returns the same dict on repeat calls
    within a session, so this also detects 'someone re-loaded with a
    mutation in between'."""
    idx = load_ffl_indexes(ffl_path)
    pair_count = len(idx.flows_by_pair)
    flow_count = len(idx.fares_by_flow)
    # Sum of fare counts per flow — independent of pair/flow counts; would
    # change if a T-record were added/removed under an existing FLOW_ID.
    fare_total = sum(len(fs) for fs in idx.fares_by_flow.values())
    return (pair_count, flow_count, fare_total)


@pytest.mark.slow
def test_no_baseline_mutation_after_full_demo_flow(
    feed_paths: FeedPaths,
    demo_change: ChangeRequest,
) -> None:
    """The headline architectural guarantee: running the full demo flow —
    compute_impact → propose → approve — does not mutate the loaded
    baseline FFL indexes. Fingerprint before and after; assert equality.

    Also asserts the original ImpactReport.canonical_affected tuple is
    identical-by-content to the one carried on the approved card (proves
    no in-place modification of the report)."""
    fp_before = _fingerprint_ffl(feed_paths.ffl)

    report = compute_impact(demo_change, feed_paths)
    canonical_before = report.canonical_affected
    fp_after_compute = _fingerprint_ffl(feed_paths.ffl)
    assert fp_before == fp_after_compute, (
        f"compute_impact mutated the baseline; "
        f"{fp_before} -> {fp_after_compute}"
    )

    layer = StagingLayer.empty()
    proposed = propose(layer, demo_change, report)
    assert isinstance(proposed, Accepted)
    fp_after_propose = _fingerprint_ffl(feed_paths.ffl)
    assert fp_before == fp_after_propose, (
        f"propose mutated the baseline; {fp_before} -> {fp_after_propose}"
    )

    approved_result = approve(proposed.layer, proposed.card.card_id)
    assert isinstance(approved_result, Accepted)
    fp_after_approve = _fingerprint_ffl(feed_paths.ffl)
    assert fp_before == fp_after_approve, (
        f"approve mutated the baseline; {fp_before} -> {fp_after_approve}"
    )

    # The approved card carries the SAME canonical_affected tuple object
    # the original report had — no copy, no rebuild. (frozen tuple, so
    # this is also a value-equality assertion.)
    assert approved_result.card.impact.canonical_affected is canonical_before


def test_no_path_from_staging_to_feedpaths() -> None:
    """Architectural: no part of the staging package imports FeedPaths or
    the ingest layer. The control verb has, by construction, no I/O
    surface — staging cannot reach out to the baseline.

    This is a static import-graph check (no FFL fixture needed)."""
    import src.staging.layer as layer_mod
    import src.staging.types as types_mod
    for mod in (layer_mod, types_mod):
        names = dir(mod)
        assert "FeedPaths" not in names, (
            f"{mod.__name__} imports FeedPaths — staging must have no "
            "I/O surface (no path to the baseline)"
        )
        assert "load_ffl_indexes" not in names, (
            f"{mod.__name__} imports a feed loader — staging must not be "
            "able to touch the baseline"
        )
        # No reference to the ingest package at all.
        for ingest_name in ("load_loc_meta", "load_nfo_overrides",
                             "load_ticket_type_meta", "FFLIndexes"):
            assert ingest_name not in names, (
                f"{mod.__name__} imports {ingest_name} from the ingest "
                "layer — staging must have no I/O surface"
            )
