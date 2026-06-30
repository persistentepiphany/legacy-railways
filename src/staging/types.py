"""Public types for the staging / control layer.

The staging layer is the "control" verb of the Conduct mapping (HACKATHON.md
§2): the place where proposed fare changes live as approval cards until a
human approves them, never silently merged, never touching the baseline.

Architectural invariants enforced by the type design (not by convention):

  - StagingLayer is frozen. `propose` and `approve` return a NEW layer;
    the input is never mutated. This is the persistent-style guarantee.
  - StagingLayer holds ONLY (ChangeRequest, ImpactReport) pairs. It has no
    references to FeedPaths, no I/O surface, no `apply` method, and does
    not import the ingest layer. There is, by construction, no path from a
    proposal to a baseline mutation (CLAUDE.md: baseline is immutable
    within a session).
  - propose/approve return a sum type — Accepted on success, Escalation on
    contradiction. Escalation is by-design, not an exception (CLAUDE.md:
    contradictions escalate, never guess). Callers (the LLM shell, the
    FastAPI surface) pattern-match on the result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

from src.impact.change_request import ChangeRequest
from src.impact.report import ImpactReport


CardStatus = Literal["pending", "approved"]


@dataclass(frozen=True)
class ApprovalCard:
    """One proposed change, addressable by `card_id` for approve/escalate.

    `impact` is the ImpactReport produced by `compute_impact(change, ...)`
    at propose time — it is what the analyst saw when deciding to approve.
    We hold it on the card so an approved-card audit shows EXACTLY the
    numbers the human saw, not a re-computed snapshot."""
    card_id: str
    change: ChangeRequest
    impact: ImpactReport
    status: CardStatus


@dataclass(frozen=True)
class StagingLayer:
    """Persistent-style staging state. Operations return a NEW layer.

    `next_card_seq` is the monotonic id source used by `propose` to mint
    the next card_id (e.g. 'card-0', 'card-1', ...). It is part of the
    layer (rather than a global counter) so two test layers can both
    start at 0 without interfering."""
    pending: tuple[ApprovalCard, ...]
    approved: tuple[ApprovalCard, ...]
    next_card_seq: int

    @classmethod
    def empty(cls) -> "StagingLayer":
        return cls(pending=(), approved=(), next_card_seq=0)

    def all_cards(self) -> tuple[ApprovalCard, ...]:
        """Pending + approved, in insertion order. Used by the contradiction
        detector — a new proposal must check against EVERY existing card,
        regardless of its approval status."""
        return self.pending + self.approved


@dataclass(frozen=True)
class ContradictingPair:
    """Two cards (or one card + one proposal) that disagree on the same
    canonical (flow_id, ticket_code) repricing.

    Each side carries a `source` (the card_id, or the sentinel "proposal"
    for an incoming change), the proposed new_price_pence, and a short
    `provenance_summary` line for the UI escalation card."""
    flow_id: str
    ticket_code: str
    option_a: dict[str, str]
    option_b: dict[str, str]


@dataclass(frozen=True)
class Escalation:
    """A contradiction was detected; the engine refuses to pick a side.

    The layer is RETURNED as part of the broader sum-type contract via the
    caller's pattern-match — but Escalation deliberately does NOT carry a
    layer field. There is no "the layer after escalation" — the original
    layer is the only sensible next state, and the caller already holds
    it. Returning it here would invite a 'just accept the new one' path."""
    reason: str
    contradictions: tuple[ContradictingPair, ...]
    proposed: ChangeRequest
    existing_card_ids: tuple[str, ...]


@dataclass(frozen=True)
class Accepted:
    """A proposal or approval was accepted into the layer.

    `layer` is the NEW StagingLayer (the input is unchanged). `card` is
    the card that was added / promoted."""
    layer: StagingLayer
    card: ApprovalCard


# The sum type callers pattern-match on. Python's typing has no native
# discriminated union for dataclasses; isinstance is the idiomatic check.
ProposalOutcome = Union[Accepted, Escalation]


__all__ = [
    "Accepted",
    "ApprovalCard",
    "CardStatus",
    "ContradictingPair",
    "Escalation",
    "ProposalOutcome",
    "StagingLayer",
]
