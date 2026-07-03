"""Structural fare-inversion detectors.

After the change is applied, three structural rules check whether the new
fare set violates customer-facing pricing logic. NONE of these use demand
data; they're pure on .TTY metadata + the new prices:

  R1 return_cheaper_than_single
      A TKT_TYPE='R' fare priced below a TKT_TYPE='S' fare with the same
      (origin, dest, TKT_CLASS, TKT_GROUP). Returns should never be
      cheaper than the equivalent single.

  R2 discounted_cheaper_than_child
      The change's discount produces a fare cheaper than the same ticket
      with the standard child discount (50% — RDG convention). The Student
      demo is engineered to trigger this — that's the headline beat.

  R3 first_cheaper_than_standard
      TKT_CLASS='1' priced <= TKT_CLASS='2' for the same (o, d, TKT_TYPE).
      The Student-on-Standard demo can't trigger this on its own; included
      so a future "discount First Class" proposal is caught.

All three group candidates by (representative_origin_nlc, representative_dest_nlc)
to avoid cross-corridor false positives. Sorted deterministically."""

from __future__ import annotations

from dataclasses import dataclass

from src.ingest.inspect import TtyRecord, load_ticket_type_meta

from src.impact.affected import AffectedFare
from src.impact.feed_paths import FeedPaths


@dataclass(frozen=True)
class FareInversion:
    """One detected inversion. `rule` identifies which detector fired —
    'return_cheaper_than_single' / 'discounted_cheaper_than_child' /
    'first_cheaper_than_standard'."""
    rule: str
    origin_nlc: str
    dest_nlc: str
    higher_ticket: str         # the one that "should be" more expensive
    higher_price_pence: int
    lower_ticket: str          # the one that "should be" cheaper but isn't
    lower_price_pence: int
    explanation: str           # short human-readable; surfaces in the UI card


def detect_inversions(
    affected: tuple[AffectedFare, ...],
    feed_paths: FeedPaths,
) -> tuple[FareInversion, ...]:
    """Run all three detectors against the canonical affected set."""
    tty = load_ticket_type_meta(feed_paths.tty)

    # Group affected by (representative_origin, representative_dest) so we
    # only compare within-corridor.
    by_corridor: dict[tuple[str, str], list[AffectedFare]] = {}
    for fare in affected:
        if fare.new_price_pence is None:
            continue
        key = (fare.representative_origin_nlc, fare.representative_dest_nlc)
        by_corridor.setdefault(key, []).append(fare)

    inversions: list[FareInversion] = []
    for (o, d), fares in by_corridor.items():
        inversions.extend(_return_cheaper_than_single(o, d, fares, tty))
        inversions.extend(_discounted_cheaper_than_child(o, d, fares, tty))
        inversions.extend(_first_cheaper_than_standard(o, d, fares, tty))

    # withdraw_product-only: for every corridor pair that lost at least one
    # fare in this change, warn if no Standard walk-up alternative remains.
    # Runs over the FULL affected set (including suppressed rows), grouped
    # by representative pair — see docstring inside the detector.
    inversions.extend(_no_standard_walkup_alternative(affected, tty))

    # Dedupe: multiple flows carrying the same ticket pair emit identical
    # rows (nested per-flow loops below); FareInversion is frozen/hashable.
    return tuple(sorted(set(inversions), key=lambda inv: (
        inv.rule, inv.origin_nlc, inv.dest_nlc, inv.higher_ticket, inv.lower_ticket,
    )))


def _return_cheaper_than_single(
    o: str, d: str, fares: list[AffectedFare], tty: dict[str, TtyRecord],
) -> list[FareInversion]:
    """A return priced below an equivalent single in the same (cls, grp)."""
    out: list[FareInversion] = []
    # Group fares by (TKT_CLASS, TKT_GROUP, TKT_TYPE) for the comparison.
    for ret in fares:
        ret_tty = tty.get(ret.ticket_code)
        if ret_tty is None or ret_tty.tkt_type != "R":
            continue
        for sgl in fares:
            if sgl is ret:
                continue
            sgl_tty = tty.get(sgl.ticket_code)
            if sgl_tty is None or sgl_tty.tkt_type != "S":
                continue
            if (sgl_tty.tkt_class != ret_tty.tkt_class
                    or sgl_tty.tkt_group != ret_tty.tkt_group):
                continue
            if ret.new_price_pence is None or sgl.new_price_pence is None:
                continue
            if ret.new_price_pence < sgl.new_price_pence:
                out.append(FareInversion(
                    rule="return_cheaper_than_single",
                    origin_nlc=o, dest_nlc=d,
                    higher_ticket=sgl.ticket_code,
                    higher_price_pence=sgl.new_price_pence,
                    lower_ticket=ret.ticket_code,
                    lower_price_pence=ret.new_price_pence,
                    explanation=(
                        f"after change: {ret.ticket_code} (return, "
                        f"{ret.new_price_pence}p) is cheaper than "
                        f"{sgl.ticket_code} (single, {sgl.new_price_pence}p) "
                        "on the same corridor + class + group"
                    ),
                ))
    return out


# RDG convention: child fares are 50% of adult (status code 001 in .DIS).
# Hard-coded here to keep the detector pure / cheap; if a future change
# touches the child discount itself, replace with a .DIS-derived value.
_CHILD_DISCOUNT_FRACTION = 0.5


def _discounted_cheaper_than_child(
    o: str, d: str, fares: list[AffectedFare], tty: dict[str, TtyRecord],
) -> list[FareInversion]:
    """For each affected ticket, compare its NEW (post-change) price against
    the same ticket's standard child discount price (computed from OLD adult).
    Flag if the proposed adult-discount yields a price below the child fare."""
    _ = tty  # this detector doesn't read .TTY fields; signature kept uniform
    out: list[FareInversion] = []
    for fare in fares:
        if fare.old_price_pence is None or fare.new_price_pence is None:
            continue
        child_pence = int(fare.old_price_pence * _CHILD_DISCOUNT_FRACTION)
        child_pence = (child_pence // 5) * 5  # same 5p flooring as the synthetic rule
        if fare.new_price_pence < child_pence:
            out.append(FareInversion(
                rule="discounted_cheaper_than_child",
                origin_nlc=o, dest_nlc=d,
                higher_ticket=f"{fare.ticket_code}-child",
                higher_price_pence=child_pence,
                lower_ticket=fare.ticket_code,
                lower_price_pence=fare.new_price_pence,
                explanation=(
                    f"proposed discount on {fare.ticket_code} ({fare.new_price_pence}p) "
                    f"is cheaper than the same ticket with the standard 50% child "
                    f"discount ({child_pence}p); adults would pay less than children"
                ),
            ))
    return out


def _first_cheaper_than_standard(
    o: str, d: str, fares: list[AffectedFare], tty: dict[str, TtyRecord],
) -> list[FareInversion]:
    """First class priced at or below standard class for the same (o, d, type)."""
    out: list[FareInversion] = []
    for first in fares:
        first_tty = tty.get(first.ticket_code)
        if first_tty is None or first_tty.tkt_class != "1":
            continue
        for std in fares:
            if std is first:
                continue
            std_tty = tty.get(std.ticket_code)
            if std_tty is None or std_tty.tkt_class != "2":
                continue
            if std_tty.tkt_type != first_tty.tkt_type:
                continue
            if first.new_price_pence is None or std.new_price_pence is None:
                continue
            if first.new_price_pence <= std.new_price_pence:
                out.append(FareInversion(
                    rule="first_cheaper_than_standard",
                    origin_nlc=o, dest_nlc=d,
                    higher_ticket=std.ticket_code,
                    higher_price_pence=std.new_price_pence,
                    lower_ticket=first.ticket_code,
                    lower_price_pence=first.new_price_pence,
                    explanation=(
                        f"after change: {first.ticket_code} (First Class, "
                        f"{first.new_price_pence}p) <= {std.ticket_code} "
                        f"(Standard, {std.new_price_pence}p) on the same corridor"
                    ),
                ))
    return out


# Advance-detection lives in the regulation classifier already but we
# don't need a full RegulationEntry lookup here — a description-substring
# check mirrors the classifier's §2 (ADVANCE) inference cheaply.
_ADVANCE_HINT = "ADVANCE"


def _is_standard_walkup(fare: AffectedFare, tty: dict[str, TtyRecord]) -> bool:
    """True if `fare` is a Standard-class walk-up single or return whose
    ticket description does NOT contain the ADVANCE hint. Mirrors the
    §1 walk-up definition from the regulation classifier without importing
    the classifier itself (kept this module cheap and self-contained)."""
    rec = tty.get(fare.ticket_code)
    if rec is None:
        return False
    if rec.tkt_class != "2":
        return False
    if rec.tkt_type not in ("S", "R"):
        return False
    if _ADVANCE_HINT in (rec.description or "").upper():
        return False
    return True


def _no_standard_walkup_alternative(
    affected: tuple[AffectedFare, ...],
    tty: dict[str, TtyRecord],
) -> list[FareInversion]:
    """Fires for withdraw_product changes only: any representative (o,d)
    pair that lost a fare in this change AND no longer has a Standard
    walk-up alternative that still has a price. The frontend reads this
    off `imp.verdict.inversions` so the review-strip counter picks it up
    without any structural change to the strip.

    Detection logic (pure, deterministic):
      1. Group `affected` by (representative_origin_nlc, representative_dest_nlc).
      2. For each group, any row whose `status='suppressed'` marks the pair
         as losing at least one fare in this change.
      3. If no non-suppressed row in the group is a Standard walk-up with a
         resolved `new_price_pence`, emit a FareInversion for the pair."""
    by_pair: dict[tuple[str, str], list[AffectedFare]] = {}
    for fare in affected:
        key = (fare.representative_origin_nlc, fare.representative_dest_nlc)
        by_pair.setdefault(key, []).append(fare)

    out: list[FareInversion] = []
    for (o, d), rows in by_pair.items():
        suppressed = [r for r in rows if r.status == "suppressed"]
        if not suppressed:
            continue
        # Alternative present iff any non-suppressed row is a Standard
        # walk-up with an int new_price. Old prices don't count — the row
        # is affected in this change, we're looking for its replacement.
        has_alt = any(
            r.status != "suppressed"
            and r.new_price_pence is not None
            and _is_standard_walkup(r, tty)
            for r in rows
        )
        if has_alt:
            continue
        withdrawn = suppressed[0]
        out.append(FareInversion(
            rule="no_standard_walkup_alternative",
            origin_nlc=o, dest_nlc=d,
            # There is no natural "higher/lower" pair here — the withdrawn
            # ticket is echoed both sides so the row still fits the
            # FareInversion shape without a schema change. The explanation
            # carries the actual story.
            higher_ticket=withdrawn.ticket_code,
            higher_price_pence=withdrawn.old_price_pence or 0,
            lower_ticket=withdrawn.ticket_code,
            lower_price_pence=0,
            explanation=(
                f"withdraw_product: {withdrawn.ticket_code} withdrawn on "
                f"{o}->{d}; no Standard-class walk-up single/return "
                "alternative remains in the affected set. Passengers on "
                "this flow would have to buy a First-class or Advance "
                "ticket instead of a walk-up Standard fare."
            ),
        ))
    return out


__all__ = ["FareInversion", "detect_inversions"]
