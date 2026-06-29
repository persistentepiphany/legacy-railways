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


def apply_synthetic_railcard(
    adult_pence: int,
    change: ChangeRequest,
) -> tuple[int, ProvenanceStep]:
    """Bulk synthetic-discount math.

    Rule (clean and citable from one line):
        discount_pence = floor(adult_pence * discount_pct)
        new_pence      = floor_to_5p(adult_pence - discount_pence)

    Floor-to-5p mirrors the BRFares-observed rounding direction
    (src/resolver/railcard.py:298-304). The provenance step records every
    input so a reviewer can hand-verify (and challenge the rule)."""
    discount = int(adult_pence * change.discount_pct)
    raw_new = adult_pence - discount
    new = (raw_new // 5) * 5
    prov = ProvenanceStep(
        step="synthetic_railcard_apply",
        source="(synthetic)",
        detail={
            "railcard_code":   change.railcard_code,
            "discount_pct":    f"{change.discount_pct:.4f}",
            "adult_pence":     str(adult_pence),
            "discount_pence":  str(discount),
            "after_discount":  str(raw_new),
            "after_round_5p":  str(new),
            "rounding":        "DOWN_5P (customer-favourable; matches BRFares oracle for real railcards)",
            "explanation":     (
                f"synthetic '{change.railcard_code}' railcard: applied "
                f"{change.discount_pct * 100:.1f}% off adult fare, "
                "floored to 5p band. No .RCM min-fare floor (deferred)."
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


__all__ = ["apply_synthetic_railcard", "inject_synthetic_railcard"]
