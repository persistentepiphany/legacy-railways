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

    inversions.sort(key=lambda inv: (
        inv.rule, inv.origin_nlc, inv.dest_nlc, inv.higher_ticket, inv.lower_ticket,
    ))
    return tuple(inversions)


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


__all__ = ["FareInversion", "detect_inversions"]
