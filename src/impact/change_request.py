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
    # --- UI-driven optional overrides ---------------------------------------
    # Left-panel controls on the delivered cockpit ("Rounding rule", "Railcard
    # min-fare floor", "Scheme area"). When None, the engine uses its feed
    # defaults (.FRR bands, .RCM floor, no cluster restriction). When present
    # they override for THIS proposal only — the baseline graph is untouched
    # (see CLAUDE.md: proposals are diffs into staging, never mutate baseline).
    rounding_rule: (
        Literal["near5", "near10", "down10", "none"] | None
    ) = None
    min_floor_pct: float | None = None    # 0 < x < 1 when present
    cluster_name: str | None = None       # human label from cluster_labels.json
    # Contradiction disposition — keyed by "<flow_id>:<ticket_code>" (the same
    # key the Escalation.contradictions list uses). A "A"/"B" per entry lets
    # the human re-propose after picking a side; the engine STILL never
    # auto-resolves anything else (unresolved contradictions on other flows
    # continue to return Escalation).
    contradiction_choice: dict[str, Literal["A", "B"]] | None = None
    # Scope of the change. "corridor" (default) targets the corridor pair
    # above; "toc" targets every flow of one operator (3-char .FFL fare-TOC
    # code, e.g. 'NTH') — corridor NLCs must then be empty strings.
    scope: Literal["corridor", "toc"] = "corridor"
    toc_code: str | None = None

    def __post_init__(self) -> None:
        # Shape-only checks here; feed-existence checks in validate_against_feed.
        if self.kind != "add_railcard":
            raise ValueError(f"unsupported ChangeRequest.kind {self.kind!r}")
        if self.scope not in ("corridor", "toc"):
            raise ValueError(f"scope must be 'corridor' or 'toc', got {self.scope!r}")
        if self.scope == "toc":
            if not (self.toc_code and 2 <= len(self.toc_code) <= 3
                    and self.toc_code.isalnum()):
                raise ValueError(
                    f"scope='toc' requires a 2-3 alnum toc_code, got {self.toc_code!r}"
                )
            if self.corridor_origin_nlc or self.corridor_dest_nlc:
                raise ValueError(
                    "scope='toc' requires empty corridor_origin_nlc/corridor_dest_nlc"
                )
        elif self.toc_code is not None:
            raise ValueError("toc_code is only valid with scope='toc'")
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
        if self.scope == "corridor":
            for label, nlc in (("origin", self.corridor_origin_nlc),
                               ("dest", self.corridor_dest_nlc)):
                if not (len(nlc) == 4 and nlc.isalnum()):
                    raise ValueError(
                        f"corridor_{label}_nlc must be 4 alnum chars, got {nlc!r}"
                    )
        if not self.description.strip():
            raise ValueError("description must not be empty")
        if self.rounding_rule is not None and self.rounding_rule not in (
            "near5", "near10", "down10", "none"
        ):
            raise ValueError(
                f"rounding_rule must be one of near5|near10|down10|none, "
                f"got {self.rounding_rule!r}"
            )
        if self.min_floor_pct is not None and not (0.0 < self.min_floor_pct < 1.0):
            raise ValueError(
                f"min_floor_pct must satisfy 0 < x < 1 strictly when set, "
                f"got {self.min_floor_pct!r}"
            )
        if self.contradiction_choice is not None:
            for key, choice in self.contradiction_choice.items():
                if choice not in ("A", "B"):
                    raise ValueError(
                        f"contradiction_choice[{key!r}] must be 'A' or 'B', "
                        f"got {choice!r}"
                    )


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

    if change.scope == "corridor":
        loc = load_loc_meta(feed_paths.loc)
        if change.corridor_origin_nlc not in loc:
            errors.append(
                f"corridor_origin_nlc {change.corridor_origin_nlc!r} not in .LOC"
            )
        if change.corridor_dest_nlc not in loc:
            errors.append(
                f"corridor_dest_nlc {change.corridor_dest_nlc!r} not in .LOC"
            )
    else:
        # TOC scope: the authoritative domain is the flows actually in the
        # .FFL — a code known to .TOC but with zero flows would be a no-op.
        # No fuzzy matching: unknown code -> typed error listing known TOCs.
        from src.ingest.inspect import load_ffl_indexes
        by_toc = load_ffl_indexes(feed_paths.ffl).flows_by_toc
        if change.toc_code not in by_toc:
            errors.append(
                f"toc_code {change.toc_code!r} has no flows in .FFL; "
                f"known fare-TOC codes: {', '.join(sorted(by_toc))}"
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
