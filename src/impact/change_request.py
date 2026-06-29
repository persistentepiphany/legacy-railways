"""The ChangeRequest dataclass — what the impact engine consumes.

A ChangeRequest is constructed directly in code (or by the LLM shell once
that lands). It NEVER carries a computed price, only the rule the analyst is
proposing. Validators reject obviously malformed inputs at construction;
boundary checks against the feed (NLC exists, ticket categories present)
live in `validate_against_feed`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.ingest.inspect import load_loc_meta, load_ticket_type_meta

from src.impact.feed_paths import FeedPaths


@dataclass(frozen=True)
class ChangeRequest:
    """A single proposed change to the fare graph.

    Currently only `kind='add_railcard'` is supported (the hackathon demo);
    new kinds (raise_price, set_restriction, ...) extend the Literal.

    `peak_valid` is captured but NOT enforced by the engine (.RST restriction
    parsing is deferred); the engine surfaces a `notes[]` entry whenever a
    proposal with `peak_valid=True` is processed so the UI can echo the
    limitation."""
    kind: Literal["add_railcard"]
    railcard_code: str                    # 3-alnum; e.g. 'STU'
    discount_pct: float                   # strict 0 < x < 1
    discount_categories: tuple[str, ...]  # .TTY DISCOUNT_CATEGORYs (2-char)
    corridor_origin_nlc: str              # 4-alnum NLC
    corridor_dest_nlc: str                # 4-alnum NLC
    peak_valid: bool                      # documentation-only — see docstring
    description: str                      # human label for the UI card

    def __post_init__(self) -> None:
        # Shape-only checks here; feed-existence checks in validate_against_feed.
        if self.kind != "add_railcard":
            raise ValueError(f"unsupported ChangeRequest.kind {self.kind!r}")
        if not (len(self.railcard_code) == 3 and self.railcard_code.isalnum()):
            raise ValueError(
                f"railcard_code must be 3 alnum chars, got {self.railcard_code!r}"
            )
        if not (0.0 < self.discount_pct < 1.0):
            raise ValueError(
                f"discount_pct must satisfy 0 < x < 1 strictly, got {self.discount_pct!r}"
            )
        if not self.discount_categories:
            raise ValueError("discount_categories must be a non-empty tuple")
        for cat in self.discount_categories:
            if not (len(cat) == 2 and cat.isalnum()):
                raise ValueError(
                    f"each discount_category must be 2 alnum chars, got {cat!r}"
                )
        for label, nlc in (("origin", self.corridor_origin_nlc),
                           ("dest", self.corridor_dest_nlc)):
            if not (len(nlc) == 4 and nlc.isalnum()):
                raise ValueError(
                    f"corridor_{label}_nlc must be 4 alnum chars, got {nlc!r}"
                )
        if not self.description.strip():
            raise ValueError("description must not be empty")


@dataclass(frozen=True)
class ValidationOutcome:
    """The boundary check against the feed. `notes` is appended to the
    ImpactReport's notes so the UI surfaces every assumption."""
    ok: bool
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def validate_against_feed(change: ChangeRequest, feed_paths: FeedPaths) -> ValidationOutcome:
    """Check the ChangeRequest's references actually exist in the loaded feed.

    Bound to the current feed snapshot (not a static check) because:
      - NLC validity depends on the LOC snapshot,
      - DISCOUNT_CATEGORYs depend on the TTY snapshot,
      - railcard_code collision depends on the RLC snapshot.

    Returns ValidationOutcome.ok=False on any violation; the caller (compute_impact)
    raises ValueError so the LLM/UI sees the failure rather than building a
    bogus report against missing entities (CLAUDE.md: no silent guesses)."""
    errors: list[str] = []
    notes: list[str] = []

    loc = load_loc_meta(feed_paths.loc)
    if change.corridor_origin_nlc not in loc:
        errors.append(
            f"corridor_origin_nlc {change.corridor_origin_nlc!r} not in .LOC"
        )
    if change.corridor_dest_nlc not in loc:
        errors.append(
            f"corridor_dest_nlc {change.corridor_dest_nlc!r} not in .LOC"
        )

    tty = load_ticket_type_meta(feed_paths.tty)
    feed_categories = {r.discount_category for r in tty.values()}
    for cat in change.discount_categories:
        if cat not in feed_categories:
            errors.append(
                f"discount_category {cat!r} not present in any .TTY row"
            )

    # Railcard-code collision check: if RLC already has this code we'd
    # silently shadow a real railcard. Only added when the rlc path resolves;
    # the synthetic-injection path also relies on this check to stay honest.
    if feed_paths.rlc.exists():
        from src.ingest.inspect import load_railcards
        existing = load_railcards(feed_paths.rlc)
        if change.railcard_code in existing:
            errors.append(
                f"railcard_code {change.railcard_code!r} already exists in .RLC "
                f"(line {existing[change.railcard_code].line_no}); pick a code "
                "not yet in the feed for the synthetic proposal"
            )

    if change.peak_valid:
        notes.append(
            "peak_valid=True captured but NOT enforced: .RST restriction-code "
            "filtering is deferred. Every fare in the affected set is treated "
            "as in-scope regardless of restriction."
        )

    return ValidationOutcome(
        ok=not errors,
        errors=tuple(errors),
        notes=tuple(notes),
    )


__all__ = ["ChangeRequest", "ValidationOutcome", "validate_against_feed"]
