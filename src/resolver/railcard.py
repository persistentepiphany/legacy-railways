"""Feed-derived railcard discount chain.

The resolver delegates the railcard discount step to this module so that
`resolve.py` stays scannable. The chain walks four feed files in order,
appending one ProvenanceStep per file read so the final fare cites the
exact .RLC/.TTY/.DIS/.RCM/.FRR lines that produced it:

    1. .RLC      → railcard's ADULT_STATUS                (RSPS5045 §4.15)
    2. .TTY      → ticket's DISCOUNT_CATEGORY             (§4.6, pos 112-113)
    3. .DIS      → (status, category) → DISCOUNT_INDICATOR + DISCOUNT_PERCENTAGE  (§4.17)
    4. apply discount per indicator (currently '0' / 'X' / 'N'; others quarantine)
    5. .RCM      → (railcard, ticket) → MIN_FARE floor    (§4.16)
    6. .FRR      → round UP per rounding band             (§4.18)

Unknown railcards, missing statuses, and unsupported indicators all return
None (price) with a ProvenanceStep explaining the miss — the resolver
turns that into status=no_fare. We never silently fall back to a default
discount (CLAUDE.md: no silent guesses).

Pure deterministic; no I/O — the indexes are passed in by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.ingest.inspect import (
    FrrBand,
    RailcardRecord,
    RcmMinFare,
    StatusDiscount,
)
from src.resolver.resolve import ProvenanceStep


# Rule "01" is the standard rounding rule for adult fares after a railcard
# discount: bands round UP to 5p across the full fare range (sub-1p rounds to
# 1p; the .FRR-defined catch-all at index 10 covers the headroom).
# RSPS5045 §4.18 doesn't bind a railcard to a rule_no anywhere in the feed,
# so we apply the published industry default and flag the choice in provenance
# so a reviewer can challenge it.
DEFAULT_RAILCARD_ROUNDING_RULE = "01"


@dataclass(frozen=True)
class RailcardOutcome:
    """Three-way result from the railcard chain.

    On success: `price_pence` set, `quarantine_reason` is None.
    On a clean miss (unknown railcard, missing DIS row, unsupported indicator):
    `price_pence` is None and `quarantine_reason` describes the miss. In both
    cases `provenance` carries the steps that should be appended to the
    resolver's chain (every file read, even those that produced the miss).
    """
    price_pence: int | None
    provenance: list[ProvenanceStep]
    quarantine_reason: str | None


def apply_railcard_from_feed(
    base_pence: int,
    railcard_code: str,
    ticket_code: str,
    *,
    railcards: dict[str, RailcardRecord],
    status_discounts: dict[tuple[str, str], StatusDiscount],
    rcm_min_fares: dict[tuple[str, str], RcmMinFare],
    frr_rules: dict[str, list[FrrBand]],
    ticket_categories: dict[str, tuple[int, str]],
    rlc_label: str = "RJFAF805.RLC",
    dis_label: str = "RJFAF805.DIS",
    rcm_label: str = "RJFAF805.RCM",
    frr_label: str = "RJFAF805.FRR",
    tty_label: str = "RJFAF805.TTY",
) -> RailcardOutcome:
    """Apply the feed-derived railcard discount chain to one adult fare.

    All indexes are pre-loaded via the inspect.py loaders so this function is
    pure CPU work and safe to call inside the resolver's hot loop."""
    prov: list[ProvenanceStep] = []

    # --- Step 1: .RLC railcard -> ADULT_STATUS ------------------------------
    rlc = railcards.get(railcard_code)
    if rlc is None:
        prov.append(ProvenanceStep(
            step="railcard_lookup",
            source=f"{rlc_label} (lookup)",
            detail={
                "railcard_code": railcard_code,
                "found":         "no",
                "explanation":   f"no .RLC record for railcard {railcard_code!r}; cannot derive status",
            },
        ))
        return RailcardOutcome(None, prov, f"unknown railcard {railcard_code!r}")
    prov.append(ProvenanceStep(
        step="railcard_lookup",
        source=f"{rlc_label} line {rlc.line_no}",
        detail={
            "RAILCARD_CODE": rlc.railcard_code,
            "DESCRIPTION":   rlc.description,
            "ADULT_STATUS":  rlc.adult_status,
            "CHILD_STATUS":  rlc.child_status,
        },
    ))

    # --- Step 2: .TTY ticket -> DISCOUNT_CATEGORY ---------------------------
    cat_entry = ticket_categories.get(ticket_code)
    if cat_entry is None:
        prov.append(ProvenanceStep(
            step="discount_category_lookup",
            source=f"{tty_label} (lookup)",
            detail={
                "ticket_code": ticket_code,
                "found":       "no",
                "explanation": f"no .TTY record for ticket {ticket_code!r}; cannot derive discount category",
            },
        ))
        return RailcardOutcome(None, prov, f"unknown ticket {ticket_code!r}")
    tty_line, discount_category = cat_entry
    prov.append(ProvenanceStep(
        step="discount_category_lookup",
        source=f"{tty_label} line {tty_line}",
        detail={
            "TICKET_CODE":       ticket_code,
            "DISCOUNT_CATEGORY": discount_category,
        },
    ))

    # --- Step 3: .DIS (status, category) -> indicator + percentage ----------
    dis = status_discounts.get((rlc.adult_status, discount_category))
    if dis is None:
        prov.append(ProvenanceStep(
            step="discount_lookup",
            source=f"{dis_label} (lookup)",
            detail={
                "status_code":       rlc.adult_status,
                "discount_category": discount_category,
                "found":             "no",
                "explanation":       (
                    f"no .DIS D-record for (status={rlc.adult_status}, "
                    f"category={discount_category}); cannot apply discount"
                ),
            },
        ))
        return RailcardOutcome(None, prov, "missing DIS row")
    prov.append(ProvenanceStep(
        step="discount_lookup",
        source=f"{dis_label} line {dis.line_no}",
        detail={
            "STATUS_CODE":         dis.status_code,
            "DISCOUNT_CATEGORY":   dis.discount_category,
            "DISCOUNT_INDICATOR":  dis.discount_indicator,
            "DISCOUNT_PERCENTAGE": f"{dis.discount_percentage} (= {dis.discount_percentage / 10:.1f}%)",
        },
    ))

    # --- Step 4: apply discount per indicator -------------------------------
    discounted = _apply_discount(base_pence, dis, prov, dis_label)
    if discounted is None:
        return RailcardOutcome(
            None, prov,
            f"unsupported DISCOUNT_INDICATOR {dis.discount_indicator!r}",
        )

    # --- Step 5: .RCM minimum-fare floor ------------------------------------
    rcm = rcm_min_fares.get((railcard_code, ticket_code))
    if rcm is None:
        prov.append(ProvenanceStep(
            step="min_fare_floor",
            source=f"{rcm_label} (lookup)",
            detail={
                "railcard_code": railcard_code,
                "ticket_code":   ticket_code,
                "found":         "no",
                "explanation":   "no .RCM row for this (railcard, ticket); no minimum-fare floor applies",
            },
        ))
    elif discounted < rcm.minimum_fare_pence:
        prov.append(ProvenanceStep(
            step="min_fare_floor",
            source=f"{rcm_label} line {rcm.line_no}",
            detail={
                "MINIMUM_FARE": str(rcm.minimum_fare_pence),
                "before":       str(discounted),
                "after":        str(rcm.minimum_fare_pence),
                "binding":      "yes",
                "explanation":  "discounted price was below .RCM minimum; raised to floor",
            },
        ))
        discounted = rcm.minimum_fare_pence
    else:
        prov.append(ProvenanceStep(
            step="min_fare_floor",
            source=f"{rcm_label} line {rcm.line_no}",
            detail={
                "MINIMUM_FARE": str(rcm.minimum_fare_pence),
                "current":      str(discounted),
                "binding":      "no",
                "explanation":  "discounted price is above the floor; no adjustment",
            },
        ))

    # --- Step 6: .FRR rounding ----------------------------------------------
    rounded = _apply_rounding(discounted, frr_rules, prov, frr_label)
    if rounded is None:
        return RailcardOutcome(
            None, prov,
            f"no FRR rule {DEFAULT_RAILCARD_ROUNDING_RULE!r} bands available",
        )

    return RailcardOutcome(rounded, prov, None)


def _apply_discount(
    base_pence: int,
    dis: StatusDiscount,
    prov: list[ProvenanceStep],
    dis_label: str,
) -> int | None:
    """Apply the discount per the DIS record's indicator. Returns the
    discounted fare in pence, or None if the indicator is unsupported."""
    ind = dis.discount_indicator
    if ind in ("X", "N"):
        prov.append(ProvenanceStep(
            step="discount_apply",
            source=f"{dis_label} line {dis.line_no}",
            detail={
                "DISCOUNT_INDICATOR": ind,
                "base":               str(base_pence),
                "discount":           "0",
                "after":              str(base_pence),
                "explanation":        "indicator 'X'/'N' = no discount applies",
            },
        ))
        return base_pence
    if ind == "0":
        # Percentage discount, integer-pence math.
        # Spec §4.17.3: DISCOUNT_PERCENTAGE is to one decimal place (334 = 33.4%).
        # Customer-favourable convention: round the discount UP so the net
        # never owes a fractional pence, matching the industry convention used
        # by BRFares for railcard-discounted fares.
        denom = 1000  # percentage is per-mille (10x %)
        num = dis.discount_percentage
        discount = (base_pence * num + denom - 1) // denom  # ceil division
        after = base_pence - discount
        prov.append(ProvenanceStep(
            step="discount_apply",
            source=f"{dis_label} line {dis.line_no}",
            detail={
                "DISCOUNT_INDICATOR":  "0 (percentage)",
                "DISCOUNT_PERCENTAGE": f"{num} (= {num / 10:.1f}%)",
                "base":                str(base_pence),
                "discount":            f"{discount} (ceil({base_pence} × {num} / {denom}))",
                "after":               str(after),
            },
        ))
        return after
    # 'F'/'M'/'H'/'L' would need the linked .DIS S-record's flat/min fares.
    # Not wired in this slice; surface as a quarantine rather than guessing.
    prov.append(ProvenanceStep(
        step="discount_apply",
        source=f"{dis_label} line {dis.line_no}",
        detail={
            "DISCOUNT_INDICATOR": ind,
            "explanation":        (
                f"indicator {ind!r} requires the linked DIS S-record's flat/min "
                "fares; not yet wired — quarantining instead of guessing"
            ),
        },
    ))
    return None


def _apply_rounding(
    fare_pence: int,
    frr_rules: dict[str, list[FrrBand]],
    prov: list[ProvenanceStep],
    frr_label: str,
) -> int | None:
    """Find the first FRR band (rule '01') whose MAX_AMOUNT >= fare and round
    UP to its ROUND_AMOUNT. Returns None if the rule is missing entirely."""
    rule_no = DEFAULT_RAILCARD_ROUNDING_RULE
    bands = frr_rules.get(rule_no, [])
    if not bands:
        prov.append(ProvenanceStep(
            step="rounding",
            source=f"{frr_label} (lookup)",
            detail={
                "rule_no":     rule_no,
                "found":       "no",
                "explanation": f"no .FRR rule {rule_no!r} bands present; cannot round",
            },
        ))
        return None
    band = next((b for b in bands if fare_pence <= b.max_amount_pence), bands[-1])
    round_to = band.round_amount_pence
    # RSPS5045 §4.18.1.1 reads "the discounted fare is rounded up to the
    # rounding amount" — but BRFares (the de-facto oracle, mirroring RDG
    # retail) empirically *floors* to the band: 9/10 sample YNG SOR rows
    # match floor and 0/10 match round-up. Customer-favourable, in line
    # with the customer-favourable ceil() on the discount itself. We follow
    # the oracle, not the spec's literal English; provenance records the
    # divergence so a reviewer can challenge it.
    if round_to <= 1 or fare_pence % round_to == 0:
        prov.append(ProvenanceStep(
            step="rounding",
            source=f"{frr_label} line {band.line_no}",
            detail={
                "rule_no":      rule_no,
                "rule_index":   band.rule_index,
                "MAX_AMOUNT":   str(band.max_amount_pence),
                "ROUND_AMOUNT": str(round_to),
                "before":       str(fare_pence),
                "after":        str(fare_pence),
                "note":         "already a multiple of the band amount; no rounding applied",
            },
        ))
        return fare_pence
    rounded = (fare_pence // round_to) * round_to
    prov.append(ProvenanceStep(
        step="rounding",
        source=f"{frr_label} line {band.line_no}",
        detail={
            "rule_no":      rule_no,
            "rule_index":   band.rule_index,
            "MAX_AMOUNT":   str(band.max_amount_pence),
            "ROUND_AMOUNT": str(round_to),
            "before":       str(fare_pence),
            "after":        str(rounded),
            "direction":    "DOWN (floor to band; matches BRFares oracle, not spec's literal 'round UP')",
        },
    ))
    return rounded


def default_feed_paths(data_dir: Path) -> dict[str, Path]:
    """Convenience: the standard {kind -> Path} mapping used when wiring the
    resolver to a data directory laid out as `data/RJFAF805.*`."""
    return {
        "rlc": data_dir / "RJFAF805.RLC",
        "dis": data_dir / "RJFAF805.DIS",
        "rcm": data_dir / "RJFAF805.RCM",
        "frr": data_dir / "RJFAF805.FRR",
        "tty": data_dir / "RJFAF805.TTY",
    }
