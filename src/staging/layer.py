"""Staging-layer operations: propose, approve, and contradiction detection.

The two public verbs:

  propose(layer, change, impact) -> Accepted | Escalation
      Add a pending card. If the incoming change's canonical repricing
      conflicts with any existing (pending OR approved) card on the same
      (flow_id, ticket_code) with a different new_price_pence, return an
      Escalation containing both options + evidence. Never silently merge,
      never pick a side (CLAUDE.md: on a contradiction, escalate).

  approve(layer, card_id) -> Accepted | Escalation
      Move a pending card to approved. The contradiction check fires
      again at approval time because two pending cards may not have
      conflicted with each other at propose time if proposed in opposite
      order (defensive — same logic on the second path).

Determinism: order of insertion is preserved. Contradictions are reported
in the order they are detected (sorted by flow_id, ticket_code for stable
diffs in test assertions)."""

from __future__ import annotations

from src.impact.change_request import ChangeRequest
from src.impact.report import ImpactReport

from src.staging.types import (
    Accepted,
    ApprovalCard,
    ContradictingPair,
    Escalation,
    ProposalOutcome,
    StagingLayer,
)


def propose(
    layer: StagingLayer,
    change: ChangeRequest,
    impact: ImpactReport,
) -> ProposalOutcome:
    """Propose a change. Returns Accepted with the NEW layer, or
    Escalation if any canonical repricing conflicts with an existing card.

    The contradiction surface is staged-vs-staged: two cards that reprice
    the same (flow_id, ticket_code) to different new_price_pence values.
    NFO-vs-staged contradictions (where a feed override fixes a price
    that a staged change would mutate) are not yet detected — deferred to
    v2 because they require deeper feed wiring."""
    contradictions = _detect_contradictions(impact, layer.all_cards())
    # Honour any A/B choices the human has already made on prior escalations
    # (delivered via change.contradiction_choice). Keys are "<flow_id>:<ticket_code>".
    # Filtering here — NOT auto-resolving — is what preserves the "engine
    # never picks a side" invariant: the human picked, we recorded, we move on.
    # Only 'B' (the proposal wins) clears a contradiction; 'A' (keep the
    # existing card) means the proposal as written still conflicts, so the
    # row stays escalated until the proposal is amended. Unknown keys are an
    # error, never silently ignored — a stale/typo'd key must not look like
    # a resolved contradiction.
    if change.contradiction_choice:
        detected_keys = {f"{cp.flow_id}:{cp.ticket_code}" for cp in contradictions}
        unknown = set(change.contradiction_choice.keys()) - detected_keys
        if unknown:
            raise ValueError(
                f"contradiction_choice key(s) {sorted(unknown)} do not match "
                f"any detected contradiction; detected keys: {sorted(detected_keys)}"
            )
        resolved = {
            key for key, pick in change.contradiction_choice.items()
            if pick == "B"
        }
        contradictions = tuple(
            cp for cp in contradictions
            if f"{cp.flow_id}:{cp.ticket_code}" not in resolved
        )
    if contradictions:
        return Escalation(
            reason=(
                f"proposed change {change.railcard_code!r} conflicts with "
                f"{len({c['source'] for cp in contradictions for c in (cp.option_a, cp.option_b)}) - 1} "
                "existing staged change(s) on the same canonical row(s); "
                "engine refuses to auto-resolve (HACKATHON.md §3 showpiece 3)"
            ),
            contradictions=contradictions,
            proposed=change,
            existing_card_ids=tuple(sorted({
                c["source"] for cp in contradictions
                for c in (cp.option_a, cp.option_b)
                if c["source"] != "proposal"
            })),
        )

    card_id = f"card-{layer.next_card_seq}"
    card = ApprovalCard(
        card_id=card_id,
        change=change,
        impact=impact,
        status="pending",
    )
    new_layer = StagingLayer(
        pending=layer.pending + (card,),
        approved=layer.approved,
        next_card_seq=layer.next_card_seq + 1,
    )
    return Accepted(layer=new_layer, card=card)


def approve(layer: StagingLayer, card_id: str) -> ProposalOutcome:
    """Move a pending card to approved.

    Returns Escalation if approving would create a conflict against an
    already-approved card. (At propose-time the new card was checked
    against all existing cards; this re-check is defensive — if cards
    were re-ordered or a defensive caller injected a card directly into
    layer.pending, the same invariant must still hold at approval.)

    Raises KeyError if the card_id isn't in pending (no silent no-op —
    CLAUDE.md: surface failures, never silently guess)."""
    pending_card: ApprovalCard | None = None
    for c in layer.pending:
        if c.card_id == card_id:
            pending_card = c
            break
    if pending_card is None:
        raise KeyError(
            f"no pending card {card_id!r}; "
            f"pending ids: {[c.card_id for c in layer.pending]}; "
            f"approved ids: {[c.card_id for c in layer.approved]}"
        )

    contradictions = _detect_contradictions(
        pending_card.impact,
        # Check against approved cards (the other pending cards already
        # passed the check at propose time, and approving one of two
        # mutually-contradicting pending cards is by-design — the analyst
        # chose). Defensive recheck against approved is what matters here.
        layer.approved,
    )
    # Honour the human's recorded A/B choices exactly as propose() does:
    # a card accepted at propose time WITH an explicit 'B' pick must not be
    # blocked here by the same contradiction the analyst already resolved.
    # (No unknown-key check: approve detects against `approved` only, so a
    # choice key from propose time may legitimately match nothing now.)
    if pending_card.change.contradiction_choice:
        resolved = {
            key for key, pick in pending_card.change.contradiction_choice.items()
            if pick == "B"
        }
        contradictions = tuple(
            cp for cp in contradictions
            if f"{cp.flow_id}:{cp.ticket_code}" not in resolved
        )
    if contradictions:
        return Escalation(
            reason=(
                f"approving card {card_id!r} would conflict with an "
                "already-approved card on the same canonical row(s)"
            ),
            contradictions=contradictions,
            proposed=pending_card.change,
            existing_card_ids=tuple(sorted({
                c["source"] for cp in contradictions
                for c in (cp.option_a, cp.option_b)
                if c["source"] != "proposal"
            })),
        )

    promoted = ApprovalCard(
        card_id=pending_card.card_id,
        change=pending_card.change,
        impact=pending_card.impact,
        status="approved",
    )
    new_pending = tuple(c for c in layer.pending if c.card_id != card_id)
    new_layer = StagingLayer(
        pending=new_pending,
        approved=layer.approved + (promoted,),
        next_card_seq=layer.next_card_seq,
    )
    return Accepted(layer=new_layer, card=promoted)


def _detect_contradictions(
    incoming: ImpactReport,
    existing: tuple[ApprovalCard, ...],
) -> tuple[ContradictingPair, ...]:
    """Same (flow_id, ticket_code) repriced to different new_price_pence
    across `incoming` and any card in `existing`.

    Same new_price = no contradiction (idempotent re-proposal). Different
    new_price = contradiction; both options surfaced with their card_id
    or the "proposal" sentinel and a short provenance summary."""
    if not existing:
        return ()

    # Index incoming canonical rows by (flow_id, ticket_code) for O(1) join.
    incoming_by_key: dict[tuple[str, str], tuple[int | None, str]] = {}
    for fare in incoming.canonical_affected:
        incoming_by_key[(fare.flow_id, fare.ticket_code)] = (
            fare.new_price_pence,
            _provenance_summary(fare),
        )

    found: list[ContradictingPair] = []
    for card in existing:
        for fare in card.impact.canonical_affected:
            key = (fare.flow_id, fare.ticket_code)
            inc = incoming_by_key.get(key)
            if inc is None:
                continue
            inc_new, inc_prov = inc
            if inc_new == fare.new_price_pence:
                continue  # idempotent — same repricing, no conflict
            found.append(ContradictingPair(
                flow_id=fare.flow_id,
                ticket_code=fare.ticket_code,
                option_a={
                    "source":              card.card_id,
                    "new_price_pence":     str(fare.new_price_pence),
                    "change_description":  card.change.description,
                    "provenance_summary":  _provenance_summary(fare),
                },
                option_b={
                    "source":              "proposal",
                    "new_price_pence":     str(inc_new),
                    "change_description":  incoming.change.description,
                    "provenance_summary":  inc_prov,
                },
            ))

    # Deterministic order — UI cards and test assertions both benefit.
    found.sort(key=lambda p: (p.flow_id, p.ticket_code))
    return tuple(found)


def _provenance_summary(fare) -> str:  # type: ignore[no-untyped-def]
    """A one-line summary of a fare's provenance chain — the steps taken,
    cited at a level suitable for an escalation card. Avoids dumping the
    full provenance into JSON; the full chain is still on the fare row
    for the rule-trace UI."""
    if not fare.provenance:
        return "(empty provenance)"
    steps = [p.step for p in fare.provenance]
    return " -> ".join(steps)


__all__ = ["approve", "propose"]
