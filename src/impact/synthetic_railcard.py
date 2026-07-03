"""Apply a synthetic railcard discount to an adult fare.

Two paths sharing the AffectedFare shape:

  apply_synthetic_railcard
      The BULK path. Given (adult_pence, ChangeRequest), returns the new
      price + one provenance step describing the synthetic-rule application.
      Used by `compute_affected_set` for every canonical row.

  inject_synthetic_railcard
      The HEADLINE path. Synthesises in-memory RailcardRecord +
      StatusDiscount records and calls `apply_railcard_from_feed` to get a
      provenance chain structurally identical to a real railcard chain
      (the demo's rule-trace showpiece reads from this chain).
      Used only for the single demo fare clicked in the UI.

Both paths skip the .RCM min-fare floor; that's surfaced in the ImpactReport
`notes[]` (a real railcard would carry an .RCM row)."""

from __future__ import annotations

from src.ingest.inspect import (
    FrrBand,
    RailcardRecord,
    RcmMinFare,
    StatusDiscount,
)
from src.resolver.railcard import RailcardOutcome, apply_railcard_from_feed
from src.resolver.resolve import ProvenanceStep

from src.impact.change_request import ChangeRequest


def _apply_ui_rounding(pence: int, rule: str | None) -> tuple[int, str]:
    """Apply the UI-selected rounding rule. Falls back to the historical
    default (floor to 5p, matching BRFares) when the ChangeRequest didn't
    override. Returns (rounded_pence, label) — the label lands in provenance
    so a reviewer can trace which rule fired."""
    if rule == "near5":
        return ((pence + 2) // 5) * 5, "NEAR_5P (round to nearest 5p)"
    if rule == "near10":
        return ((pence + 5) // 10) * 10, "NEAR_10P (round to nearest 10p)"
    if rule == "down10":
        return (pence // 10) * 10, "DOWN_10P (floor to 10p band)"
    if rule == "none":
        return pence, "NONE (exact pence — no rounding)"
    # No override — historical default (floor to 5p) preserved.
    return (pence // 5) * 5, "DOWN_5P (default; matches BRFares oracle)"


def apply_synthetic_railcard(
    adult_pence: int,
    change: ChangeRequest,
) -> tuple[int, ProvenanceStep]:
    """Bulk synthetic-discount math.

    Rule (clean and citable from one line):
        discount_pence = floor(adult_pence * discount_pct)
        new_pence      = round_to_rule(adult_pence - discount_pence)
        new_pence      = max(new_pence, floor)     when the proposal sets one

    Default rounding is floor-to-5p (matches BRFares empirically). The
    ChangeRequest's `rounding_rule` and `min_floor_pct` overrides are
    honoured for THIS proposal only — the baseline graph is untouched
    (CLAUDE.md: proposals are diffs into staging).

    kind='raise_price' reuses the same math with the sign flipped:
        new_pence = round_to_rule(adult_pence + floor(adult_pence * pct))
    This is what the compliance join tests against the §3 cap — a rise on a
    regulated ticket is exactly the breach the 0% freeze forbids."""
    is_rise = change.kind == "raise_price"
    delta = int(adult_pence * change.discount_pct)
    raw_new = adult_pence + delta if is_rise else adult_pence - delta
    new, rounding_label = _apply_ui_rounding(raw_new, change.rounding_rule)
    floor_clamp: int | None = None
    floor_applied = False
    if change.min_floor_pct is not None:
        # Floor is a fraction of the pre-discount adult fare: a UI-driven
        # railcard cannot fall below `min_floor_pct` of the protected price.
        floor_clamp = int(adult_pence * change.min_floor_pct)
        if new < floor_clamp:
            new = floor_clamp
            floor_applied = True
    prov = ProvenanceStep(
        step="synthetic_railcard_apply",
        source="(synthetic)",
        detail={
            "railcard_code":   change.railcard_code,
            "kind":            change.kind,
            "discount_pct":    f"{change.discount_pct:.4f}",
            "adult_pence":     str(adult_pence),
            "discount_pence":  f"+{delta}" if is_rise else str(delta),
            "after_discount":  str(raw_new),
            "after_round":     str(new if not floor_applied else _apply_ui_rounding(raw_new, change.rounding_rule)[0]),
            "rounding":        rounding_label,
            "min_floor_pct":   f"{change.min_floor_pct:.4f}" if change.min_floor_pct is not None else "(unset)",
            "floor_pence":     str(floor_clamp) if floor_clamp is not None else "(none)",
            "floor_binding":   "yes" if floor_applied else "no",
            "final":           str(new),
            "explanation":     (
                (
                    f"proposed fare rise '{change.railcard_code}': applied "
                    f"+{change.discount_pct * 100:.1f}% to adult fare, "
                    f"rounding={rounding_label}."
                ) if is_rise else (
                    f"synthetic '{change.railcard_code}' railcard: applied "
                    f"{change.discount_pct * 100:.1f}% off adult fare, "
                    f"rounding={rounding_label}. "
                    + ("Min-fare floor bound the result." if floor_applied
                       else "No .RCM min-fare floor (deferred).")
                )
            ),
        },
    )
    return new, prov


# --- Injected (headline) path ----------------------------------------------
# Build an in-memory railcard chain and call apply_railcard_from_feed so the
# headline demo fare's provenance has the same shape as a real railcard
# chain. Reviewers cannot tell from the chain structure whether it came
# from the feed or the proposal — that's the point.


_SYNTHETIC_STATUS_CODE = "SYN"  # 3-char; cannot collide with real status codes (numeric in feed)
_SYNTHETIC_RLC_LINE_NO = -1
_SYNTHETIC_DIS_LINE_NO = -2


def _synthetic_railcard_record(change: ChangeRequest) -> RailcardRecord:
    """One synthetic .RLC row representing the proposal."""
    return RailcardRecord(
        line_no=_SYNTHETIC_RLC_LINE_NO,
        railcard_code=change.railcard_code,
        description=change.description[:20],
        adult_status=_SYNTHETIC_STATUS_CODE,
        child_status="XXX",          # not proposing a child variant in this slice
        min_passengers=1,
        max_passengers=1,
        end_date="31122999",
        start_date="01032026",
    )


def _synthetic_status_discounts(
    change: ChangeRequest,
) -> dict[tuple[str, str], StatusDiscount]:
    """One synthetic .DIS D-row per discount_category. DISCOUNT_PERCENTAGE is
    in per-mille (334 = 33.4%); we round the float to int per-mille so the
    chain produces an integer that can flow through apply_discount."""
    per_mille = int(round(change.discount_pct * 1000))
    out: dict[tuple[str, str], StatusDiscount] = {}
    for cat in change.discount_categories:
        out[(_SYNTHETIC_STATUS_CODE, cat)] = StatusDiscount(
            line_no=_SYNTHETIC_DIS_LINE_NO,
            status_code=_SYNTHETIC_STATUS_CODE,
            discount_category=cat,
            discount_indicator="0",
            discount_percentage=per_mille,
            end_date="31122999",
        )
    return out


def inject_synthetic_railcard(
    adult_pence: int,
    change: ChangeRequest,
    *,
    ticket_code: str,
    ticket_categories: dict[str, tuple[int, str]],
    frr_rules: dict[str, list[FrrBand]],
    rcm_min_fares: dict[tuple[str, str], RcmMinFare] | None = None,
) -> RailcardOutcome:
    """The "structurally identical to a real railcard chain" path.

    Merges a synthetic RailcardRecord + StatusDiscount into the live indexes,
    then calls apply_railcard_from_feed. The returned RailcardOutcome carries
    the full provenance: railcard_lookup, discount_category_lookup,
    discount_lookup, discount_apply, min_fare_floor, rounding — exactly the
    chain a YNG fare would produce, with `(synthetic)` markers where the
    rows came from the proposal."""
    railcards = {change.railcard_code: _synthetic_railcard_record(change)}
    status_discounts = _synthetic_status_discounts(change)
    return apply_railcard_from_feed(
        base_pence=adult_pence,
        railcard_code=change.railcard_code,
        ticket_code=ticket_code,
        railcards=railcards,
        status_discounts=status_discounts,
        rcm_min_fares=rcm_min_fares or {},
        frr_rules=frr_rules,
        ticket_categories=ticket_categories,
        rlc_label="(synthetic).RLC",
        dis_label="(synthetic).DIS",
        rcm_label="RJFAF805.RCM",  # real RCM lookups still cite the real file
        frr_label="RJFAF805.FRR",
        tty_label="RJFAF805.TTY",
    )


def apply_cap_price(
    adult_pence: int,
    change: ChangeRequest,
) -> tuple[int, ProvenanceStep]:
    """Bulk apply_cap math — signed percentage delta on a regulated fare.

    Rule:
        raw_new  = adult_pence + floor(adult_pence * cap_pct)
        new_pence = round_to_rule(raw_new)
        new_pence = max(0, new_pence)

    A cap_pct of 0.0 is a freeze — the input is echoed byte-for-byte (the
    rounding rule still fires, so a fare currently sitting on a non-band
    price would still snap; this is deliberate — the regulator's cap is a
    ceiling on the rounded price). This helper is called for every affected
    (regulated) fare in the apply_cap corridor walk (`_compute_affected_
    set_apply_cap_corridor`)."""
    if change.cap_pct is None:  # defensive — validated at construction
        raise ValueError("apply_cap_price called with cap_pct=None")
    delta = int(adult_pence * change.cap_pct)
    raw_new = adult_pence + delta
    if raw_new < 0:
        raw_new = 0
    new, rounding_label = _apply_ui_rounding(raw_new, change.rounding_rule)
    prov = ProvenanceStep(
        step="cap_apply",
        source="(synthetic)",
        detail={
            "cap_pct":         f"{change.cap_pct:.4f}",
            "adult_pence":     str(adult_pence),
            "delta_pence":     str(delta),
            "after_delta":     str(raw_new),
            "after_round":     str(new),
            "rounding":        rounding_label,
            "final":           str(new),
            "explanation":     (
                f"apply_cap: {change.cap_pct * 100:+.1f}% on regulated fare "
                f"{adult_pence}p → {new}p (rounded via {rounding_label}). "
                "Unregulated fares in scope are excluded from the affected "
                "set — see the report's notes[] for the count."
            ),
        },
    )
    return new, prov


def apply_adjust_price(
    adult_pence: int,
    change: ChangeRequest,
) -> tuple[int, ProvenanceStep]:
    """Bulk adjust_fares math — signed pct OR absolute pence delta.

    Rule:
        raw_new = adult_pence * (1 + delta_value)         [pct mode]
        raw_new = adult_pence + int(delta_value)          [pence mode]
        new_pence = max(0, round_to_rule(raw_new))

    Reused for every ticket in the change's basket; the basket filter
    happens upstream (only fares whose ticket_code ∈ change.tickets reach
    this helper)."""
    if change.delta_mode is None or change.delta_value is None:
        raise ValueError("apply_adjust_price called with delta_mode/delta_value=None")
    if change.delta_mode == "pct":
        raw_new = int(adult_pence * (1.0 + change.delta_value))
        delta_label = f"{change.delta_value * 100:+.1f}%"
    else:
        raw_new = adult_pence + int(change.delta_value)
        delta_label = f"{int(change.delta_value):+d}p"
    if raw_new < 0:
        raw_new = 0
    new, rounding_label = _apply_ui_rounding(raw_new, change.rounding_rule)
    prov = ProvenanceStep(
        step="adjust_apply",
        source="(synthetic)",
        detail={
            "delta_mode":      change.delta_mode,
            "delta_value":     f"{change.delta_value:.4f}",
            "delta_label":     delta_label,
            "adult_pence":     str(adult_pence),
            "after_delta":     str(raw_new),
            "after_round":     str(new),
            "rounding":        rounding_label,
            "final":           str(new),
            "explanation":     (
                f"adjust_fares: {delta_label} on {adult_pence}p → {new}p "
                f"(rounded via {rounding_label}). Basket filter applied "
                "upstream; only tickets in change.tickets reach this row."
            ),
        },
    )
    return new, prov


def apply_withdrawal(
    adult_pence: int,
    change: ChangeRequest,
) -> tuple[int | None, ProvenanceStep]:
    """withdraw_product path — mark the fare withdrawn.

    Returns `(None, prov_step)` — the new price is INTENTIONALLY None (not
    zero) so downstream consumers (report, adapters, compliance) see an
    honest suppression rather than a fabricated £0 price. Mirrors the
    `.NFO` sentinel discipline (see CLAUDE.md: on bad/ambiguous data the
    resolver NEVER silently guesses, and here 'no fare' is the intent)."""
    prov = ProvenanceStep(
        step="withdraw_apply",
        source="(synthetic)",
        detail={
            "ticket_code":    change.withdraw_ticket or "",
            "adult_pence":    str(adult_pence),
            "new_pence":      "(withdrawn)",
            "final":          "(withdrawn)",
            "explanation":    (
                f"withdraw_product: {change.withdraw_ticket!r} removed from the "
                f"scope's flow set. Passengers on affected flows will have to "
                "pay the next-cheapest valid ticket; no synthetic price was "
                "computed for this row (honest suppression, not a £0 fare)."
            ),
        },
    )
    return None, prov


__all__ = [
    "apply_adjust_price",
    "apply_cap_price",
    "apply_synthetic_railcard",
    "apply_withdrawal",
    "inject_synthetic_railcard",
]
