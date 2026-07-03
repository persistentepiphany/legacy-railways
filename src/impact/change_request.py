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


ChangeKind = Literal[
    "add_railcard", "raise_price", "apply_cap", "adjust_fares", "withdraw_product"
]
DeltaMode = Literal["pct", "pence"]


@dataclass(frozen=True)
class ChangeRequest:
    """A single proposed change to the fare graph.

    Five kinds:
      - "add_railcard"     — the hackathon demo: propose a synthetic railcard
        with a % discount over one or more .TTY DISCOUNT_CATEGORYs.
      - "raise_price"      — across-the-board price rise on the scoped tickets.
        `discount_pct` is reused as the INCREASE fraction (0.05 = +5%);
        `railcard_code` is the proposal's synthetic identifier only (no
        .RLC row is implied, so the collision check is skipped).
      - "apply_cap"        — apply a signed percentage delta (negative, zero, or
        positive) to every REGULATED fare within scope. Unregulated fares are
        left untouched; the report's `notes[]` carries the count.
      - "adjust_fares"     — % or pence delta on a ticket-type basket.
      - "withdraw_product" — remove a ticket product within scope (requires
        explicit `confirmed=True`).

    Kind-specific fields are optional at the dataclass level and validated
    per-kind in `__post_init__`.

    `peak_valid` is captured but NOT enforced by the engine (.RST restriction
    parsing is deferred); the engine surfaces a `notes[]` entry whenever a
    proposal with `peak_valid=True` is processed so the UI can echo the
    limitation."""
    kind: ChangeKind
    corridor_origin_nlc: str              # 4-alnum NLC (empty when scope='toc')
    corridor_dest_nlc: str                # 4-alnum NLC (empty when scope='toc')
    peak_valid: bool                      # documentation-only — see docstring
    description: str                      # human label for the UI card
    # --- add_railcard fields (required when kind='add_railcard') --------
    railcard_code: str = ""                        # 3-alnum; e.g. 'STU'
    discount_pct: float = 0.0                      # strict 0 < x < 1
    discount_categories: tuple[str, ...] = ()      # .TTY DISCOUNT_CATEGORYs (2-char)
    # --- apply_cap fields (required when kind='apply_cap') --------------
    # Signed multiplier on the *current* fare — a cap of 0 freezes it,
    # negative reduces, positive raises. Range check keeps it in a plausible
    # regulated-fares band (±25%).
    cap_pct: float | None = None
    # --- adjust_fares fields (required when kind='adjust_fares') --------
    # Ticket-type basket (one or more 3-alnum codes from .TTY) plus a delta.
    # `delta_mode` selects whether `delta_value` is a fraction (0.03 = +3%)
    # or an absolute pence offset (300 = +£3). The engine floors to non-
    # negative pence in either mode; the rounding rule then applies.
    tickets: tuple[str, ...] = ()
    delta_mode: DeltaMode | None = None
    delta_value: float | None = None
    # --- withdraw_product fields (required when kind='withdraw_product') --
    # A single ticket code to withdraw within scope. `confirmed` is a
    # tri-state guard on the API surface — the frontend forces the user to
    # tick the confirmation before Run impact / propose fires; the engine
    # rejects a payload without it so a stray unconfirmed API call cannot
    # silently withdraw fares.
    withdraw_ticket: str | None = None
    confirmed: bool = False
    # --- UI-driven optional overrides -----------------------------------
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
        if self.kind not in (
            "add_railcard", "raise_price",
            "apply_cap", "adjust_fares", "withdraw_product",
        ):
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
        if self.scope == "corridor":
            for label, nlc in (("origin", self.corridor_origin_nlc),
                               ("dest", self.corridor_dest_nlc)):
                if not (len(nlc) == 4 and nlc.isalnum()):
                    raise ValueError(
                        f"corridor_{label}_nlc must be 4 alnum chars, got {nlc!r}"
                    )
        if not self.description.strip():
            raise ValueError("description must not be empty")
        # raise_price reuses the add_railcard field shapes (discount_pct is the
        # increase fraction; railcard_code is the synthetic identifier).
        if self.kind in ("add_railcard", "raise_price"):
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
            if self.cap_pct is not None:
                raise ValueError(
                    "cap_pct is only valid with kind='apply_cap'"
                )
            if self.tickets:
                raise ValueError(
                    "tickets is only valid with kind='adjust_fares' or 'withdraw_product'"
                )
            if self.delta_mode is not None or self.delta_value is not None:
                raise ValueError(
                    "delta_mode/delta_value are only valid with kind='adjust_fares'"
                )
            if self.withdraw_ticket is not None:
                raise ValueError(
                    "withdraw_ticket is only valid with kind='withdraw_product'"
                )
        elif self.kind == "apply_cap":
            if self.cap_pct is None:
                raise ValueError("cap_pct is required when kind='apply_cap'")
            if not (-0.25 <= self.cap_pct <= 0.25):
                raise ValueError(
                    f"cap_pct must satisfy -0.25 <= x <= 0.25 (±25%), "
                    f"got {self.cap_pct!r}"
                )
            for other, name in (
                (self.railcard_code, "railcard_code"),
                (self.discount_categories, "discount_categories"),
                (self.tickets, "tickets"),
            ):
                if other:
                    raise ValueError(
                        f"{name} must be empty when kind='apply_cap'"
                    )
            if self.discount_pct != 0.0:
                raise ValueError(
                    "discount_pct must be 0.0 when kind='apply_cap'"
                )
            if self.delta_mode is not None or self.delta_value is not None:
                raise ValueError(
                    "delta_mode/delta_value are only valid with kind='adjust_fares'"
                )
        elif self.kind == "adjust_fares":
            if not self.tickets:
                raise ValueError(
                    "tickets must be a non-empty tuple when kind='adjust_fares'"
                )
            for t in self.tickets:
                if not (len(t) == 3 and t.isalnum()):
                    raise ValueError(
                        f"each tickets entry must be 3 alnum chars, got {t!r}"
                    )
            if self.delta_mode not in ("pct", "pence"):
                raise ValueError(
                    f"delta_mode must be 'pct' or 'pence', got {self.delta_mode!r}"
                )
            if self.delta_value is None:
                raise ValueError(
                    "delta_value is required when kind='adjust_fares'"
                )
            if self.delta_mode == "pct" and not (-0.5 <= self.delta_value <= 0.5):
                raise ValueError(
                    f"delta_value must satisfy -0.5 <= x <= 0.5 for pct mode "
                    f"(±50%), got {self.delta_value!r}"
                )
            if self.delta_mode == "pence" and not (-10_000 <= self.delta_value <= 10_000):
                raise ValueError(
                    f"delta_value must satisfy -10000 <= x <= 10000 for pence mode, "
                    f"got {self.delta_value!r}"
                )
            for other, name in (
                (self.railcard_code, "railcard_code"),
                (self.discount_categories, "discount_categories"),
            ):
                if other:
                    raise ValueError(
                        f"{name} must be empty when kind='adjust_fares'"
                    )
            if self.discount_pct != 0.0:
                raise ValueError(
                    "discount_pct must be 0.0 when kind='adjust_fares'"
                )
            if self.cap_pct is not None:
                raise ValueError(
                    "cap_pct is only valid with kind='apply_cap'"
                )
            if self.withdraw_ticket is not None:
                raise ValueError(
                    "withdraw_ticket is only valid with kind='withdraw_product'"
                )
        elif self.kind == "withdraw_product":
            if self.withdraw_ticket is None:
                raise ValueError(
                    "withdraw_ticket is required when kind='withdraw_product'"
                )
            if not (
                len(self.withdraw_ticket) == 3 and self.withdraw_ticket.isalnum()
            ):
                raise ValueError(
                    f"withdraw_ticket must be 3 alnum chars, "
                    f"got {self.withdraw_ticket!r}"
                )
            if not self.confirmed:
                raise ValueError(
                    "withdraw_product requires confirmed=True (analyst "
                    "acknowledgement that the fare will disappear)"
                )
            for other, name in (
                (self.railcard_code, "railcard_code"),
                (self.discount_categories, "discount_categories"),
                (self.tickets, "tickets"),
            ):
                if other:
                    raise ValueError(
                        f"{name} must be empty when kind='withdraw_product'"
                    )
            if self.discount_pct != 0.0:
                raise ValueError(
                    "discount_pct must be 0.0 when kind='withdraw_product'"
                )
            if self.cap_pct is not None:
                raise ValueError(
                    "cap_pct is only valid with kind='apply_cap'"
                )
            if self.delta_mode is not None or self.delta_value is not None:
                raise ValueError(
                    "delta_mode/delta_value are only valid with kind='adjust_fares'"
                )
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

    # adjust_fares / withdraw_product: each cited ticket_code must exist in .TTY.
    if change.kind in ("adjust_fares", "withdraw_product"):
        tty = load_ticket_type_meta(feed_paths.tty)
        cited: list[str] = list(change.tickets)
        if change.kind == "withdraw_product" and change.withdraw_ticket:
            cited.append(change.withdraw_ticket)
        for code in cited:
            if code not in tty:
                errors.append(
                    f"ticket_code {code!r} not present in .TTY"
                )

    # Category presence: add_railcard AND raise_price both scope by category.
    if change.kind in ("add_railcard", "raise_price"):
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
    # raise_price proposals create no railcard, so no shadowing is possible.
    if change.kind == "add_railcard" and feed_paths.rlc.exists():
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
