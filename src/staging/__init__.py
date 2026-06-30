"""Staging / control layer — the "control" verb (HACKATHON.md §2).

Architectural guarantees this package upholds:
  - The baseline fare graph is immutable within a session. There is no
    code path from a staged proposal (or from the future LLM shell) to a
    baseline mutation. The layer holds only (ChangeRequest, ImpactReport)
    pairs; it has no FeedPaths reference and no I/O surface.
  - Approval appends to staging only; nothing is silently merged. The
    contradiction check fires at propose AND approve time.
  - Contradictions ESCALATE (return Escalation), never auto-resolve. The
    Escalation object carries both options with their evidence — the
    human picks (CLAUDE.md: never silently guess).

Public API:
    StagingLayer       — persistent-style staging state
    ApprovalCard       — one (change, impact) on the queue
    propose            — add a pending card, or escalate
    approve            — promote a pending card to approved, or escalate
    Accepted, Escalation, ProposalOutcome — sum-type returns
    ContradictingPair  — one row that disagrees across cards
"""

from src.staging.layer import approve, propose
from src.staging.types import (
    Accepted,
    ApprovalCard,
    CardStatus,
    ContradictingPair,
    Escalation,
    ProposalOutcome,
    StagingLayer,
)

__all__ = [
    "Accepted",
    "ApprovalCard",
    "CardStatus",
    "ContradictingPair",
    "Escalation",
    "ProposalOutcome",
    "StagingLayer",
    "approve",
    "propose",
]
