"""Compliance join: AffectedFare × RegulationMap → ComplianceVerdict.

The regulation map (src/regulation/) and the impact engine (src/impact/
affected.py) are built and tested independently. This module is the JOIN
that turns "we know which fares are repriced" + "we know which fares are
regulated and at what cap" into the demo's red/amber/green compliance flag
on every affected row.

Pure, deterministic, side-effect-free. No I/O beyond reading the already-
loaded RegulationMap. The check is a single inequality (REGULATION.md §3):

    new_price_pence > cap_price_2025_pence   →  BREACH

Boundary is strict `>`, not `>=`: the cap is a price ceiling, hitting the
cap exactly is compliant. (The cap mechanism allows "up to" — see §3.)

`compliance` is attached as a separate field on AffectedFare, NOT appended
to the resolver-provenance chain. The provenance chain describes HOW the
price was computed; compliance is a downstream CLASSIFICATION of that
price against an external rule. Mixing them would also break existing
provenance-shape assertions (tests/test_impact_demo_corridor.py)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from src.ingest.inspect import LocationMeta, load_loc_meta

from src.impact.affected import AffectedFare, AffectedSet
from src.impact.change_request import ChangeRequest
from src.impact.feed_paths import FeedPaths
from src.regulation import (
    CorridorSpec,
    RegulationCitation,
    RegulationEntry,
    RegulationMap,
    build_regulation_map,
)


ComplianceStatus = Literal["compliant", "breach", "not_regulated"]


@dataclass(frozen=True)
class ComplianceVerdict:
    """One row's compliance classification against the 0% freeze (REGULATION.md §3).

    `cap_price_2025_pence` is None when status='not_regulated' (no cap to compare).
    `citation` is always present when the regmap had an entry — even an
    HONEST GAP (MISSING) entry, so the UI can surface "this looks like a
    regulated walk-up but it isn't in the corridor's .FFL" honestly rather
    than silently dropping the row.
    `new_price_pence` is echoed so consumers (UI, JSON serializers, the
    later LLM shell) have a self-contained verdict without re-joining."""
    status: ComplianceStatus
    cap_price_2025_pence: int | None
    new_price_pence: int
    citation: RegulationCitation | None
    explanation: str


# London terminal NLCs and the London-terminals group NLC. This is the
# hardcoded inference for `is_london_flow` — used by the regulation
# classifier's REGULATED_WALKUPS_LONDON rule (R6 in src/regulation/classify.py).
# Sourced from .LOC inspection on the RJFAF805 snapshot. Documented as a
# known limitation: the v2 fix is to derive this from .LOC FARE_GROUP /
# COUNTY rather than a hardcoded set.
_LONDON_TERMINAL_NLCS: frozenset[str] = frozenset({
    "1072",  # LONDON TERMINALS group NLC
    "1444",  # EUSTON
    "1432",  # KINGS CROSS
    "1428",  # ST PANCRAS
    "1488",  # PADDINGTON
    "1456",  # LIVERPOOL ST
    "1480",  # MARYLEBONE
    "1492",  # VICTORIA
    "1448",  # WATERLOO
    "1408",  # CHARING CROSS
    "1424",  # FENCHURCH ST
    "1460",  # CANNON ST
    "1452",  # LONDON BRIDGE
})


def _infer_london_flow(
    origin_nlc: str,
    dest_nlc: str,
    loc: dict[str, LocationMeta],
) -> bool:
    """True if either endpoint is a London terminal (or in the London
    terminals group). Drives the REGULATED_WALKUPS_LONDON rule in
    classify_ticket. Hardcoded list — v2 should derive from .LOC."""
    for nlc in (origin_nlc, dest_nlc):
        if nlc in _LONDON_TERMINAL_NLCS:
            return True
        meta = loc.get(nlc)
        if meta is not None and meta.group_nlc in _LONDON_TERMINAL_NLCS:
            return True
    return False


def build_corridor_regulation_map(
    change: ChangeRequest,
    feed_paths: FeedPaths,
) -> RegulationMap:
    """Build a one-corridor regulation map for the change's (origin, dest).

    Convenience wrapper around `src.regulation.build_regulation_map` that
    fills in CorridorSpec from a ChangeRequest. `is_london_flow` is
    inferred via `_infer_london_flow` (hardcoded London-terminals set)."""
    loc = load_loc_meta(feed_paths.loc)
    is_london = _infer_london_flow(
        change.corridor_origin_nlc, change.corridor_dest_nlc, loc,
    )
    corridor = CorridorSpec(
        name=f"{change.corridor_origin_nlc}-{change.corridor_dest_nlc}",
        origin_nlc=change.corridor_origin_nlc,
        dest_nlc=change.corridor_dest_nlc,
        is_london_flow=is_london,
    )
    return build_regulation_map(
        [corridor],
        ffl_path=feed_paths.ffl,
        loc_path=feed_paths.loc,
        tty_path=feed_paths.tty,
        fsc_path=feed_paths.fsc,
    )


def check_compliance(
    fare: AffectedFare,
    regmap: RegulationMap,
    *,
    corridor_origin_nlc: str,
    corridor_dest_nlc: str,
) -> ComplianceVerdict:
    """Classify one row against the regulation map.

    Lookup key is the CORRIDOR NLCs from the ChangeRequest, NOT
    `fare.representative_origin_nlc/dest_nlc`. Affected rows produced by
    LOC group fan-out carry the *group* NLC (e.g. '0438', '1072') as their
    representative, while the regulation map is keyed by the corridor NLCs
    the analyst specified (e.g. '2968', '1444'). A regulated walk-up like
    SVR on MAN-EUS lives at `(2968, 1444, 'SVR')` in the map regardless of
    which expansion produced the flow.

    Status decision (REGULATION.md §3):
      - regmap entry missing OR entry.regulated=False  → not_regulated
      - regulated AND new_price_pence > effective cap  → breach
      - otherwise                                       → compliant

    The effective cap is max(map cap, the fare's own old price): the map's
    §4 fallback cap is the corridor-cheapest fare per ticket, which a
    pricier route exceeds even pre-change; the fare's own current price is
    its fallback baseline, so a decrease never breaches.

    Boundary is strict `>` — a fare priced AT the cap is compliant (the cap
    is a ceiling, not an upper-exclusive bound)."""
    # AffectedFare can in principle carry new_price_pence=None when status
    # != 'resolved'. In the current bulk path every canonical row is
    # 'resolved' with an int price, but be defensive.
    new_price = fare.new_price_pence
    if new_price is None:
        return ComplianceVerdict(
            status="not_regulated",
            cap_price_2025_pence=None,
            new_price_pence=0,
            citation=None,
            explanation=(
                "row has no new_price_pence (status != 'resolved'); "
                "compliance not evaluated"
            ),
        )

    entry: RegulationEntry | None = regmap.get(
        corridor_origin_nlc, corridor_dest_nlc, fare.ticket_code,
    )

    if entry is None:
        return ComplianceVerdict(
            status="not_regulated",
            cap_price_2025_pence=None,
            new_price_pence=new_price,
            citation=None,
            explanation=(
                f"no regulation_map entry for "
                f"({fare.representative_origin_nlc},"
                f"{fare.representative_dest_nlc},{fare.ticket_code}); "
                "treated as not regulated"
            ),
        )

    if not entry.regulated:
        # Echo the citation even though we say not_regulated — this is how
        # the UI knows whether the row was unregulated by rule (e.g.
        # Advance/First Class) or unregulated by honest gap (MISSING:).
        return ComplianceVerdict(
            status="not_regulated",
            cap_price_2025_pence=None,
            new_price_pence=new_price,
            citation=entry.citation,
            explanation=(
                f"not regulated under {entry.citation.section}: "
                f"{entry.citation.rule_text}"
            ),
        )

    cap = entry.cap_price_2025_pence
    if cap is None:
        # Defensive: regulated=True with no cap shouldn't happen in the
        # current map (cap is set whenever regulated is) but if it does we
        # cannot compare — surface as not_regulated with the citation so
        # the gap is visible rather than silently passing.
        return ComplianceVerdict(
            status="not_regulated",
            cap_price_2025_pence=None,
            new_price_pence=new_price,
            citation=entry.citation,
            explanation=(
                f"regulated under {entry.citation.section} but no "
                "cap_price_2025_pence available; cannot evaluate compliance"
            ),
        )

    # The map's fallback cap is the cheapest current fare for this ticket
    # ACROSS ALL ROUTES on the corridor (REGULATION.md §4 fallback). A fare
    # on a pricier route would "exceed" that cap even before the change. The
    # §4 fallback treats *the fare's own current price* as its frozen
    # baseline, so the effective cap for this row is max(map cap, old price):
    # a decrease can never breach; an increase above the fare's own current
    # price still does (0% freeze).
    old_price = fare.old_price_pence
    effective_cap = cap if old_price is None else max(cap, old_price)
    cap_note = ""
    if effective_cap != cap:
        cap_note = (
            f" (map cap {cap}p is the corridor-cheapest fallback for this "
            f"ticket; this fare's own current price {old_price}p is its "
            "§4 fallback baseline)"
        )

    if new_price > effective_cap:
        overage = new_price - effective_cap
        return ComplianceVerdict(
            status="breach",
            cap_price_2025_pence=effective_cap,
            new_price_pence=new_price,
            citation=entry.citation,
            explanation=(
                f"BREACH: new_price {new_price}p exceeds 1 Mar 2025 cap "
                f"{effective_cap}p by {overage}p{cap_note}. Regulated under "
                f"{entry.citation.section} ({entry.citation.rule_text}); "
                "the 0% freeze (REGULATION.md §3) forbids any increase."
            ),
        )

    return ComplianceVerdict(
        status="compliant",
        cap_price_2025_pence=effective_cap,
        new_price_pence=new_price,
        citation=entry.citation,
        explanation=(
            f"compliant: new_price {new_price}p <= cap {effective_cap}p"
            f"{cap_note}. Regulated under {entry.citation.section} "
            f"({entry.citation.rule_text})."
        ),
    )


def attach_compliance(
    affected: AffectedSet,
    regmap: RegulationMap,
    *,
    corridor_origin_nlc: str,
    corridor_dest_nlc: str,
) -> AffectedSet:
    """Return a new AffectedSet whose canonical rows carry compliance verdicts.

    Lookup uses the CORRIDOR NLCs (passed in from the ChangeRequest), not
    the row's `representative_origin/dest_nlc` (which may be a group NLC
    post-fan-out). See check_compliance docstring.

    Resolver-provenance on each row is untouched — compliance is a separate
    field, not an extra step. The skipped/blast_radius/notes are passed
    through unchanged."""
    enriched = tuple(
        replace(
            fare,
            compliance=check_compliance(
                fare, regmap,
                corridor_origin_nlc=corridor_origin_nlc,
                corridor_dest_nlc=corridor_dest_nlc,
            ),
        )
        for fare in affected.canonical
    )
    # Replace on AffectedSet too — it's frozen.
    return replace(affected, canonical=enriched)


__all__ = [
    "ComplianceStatus",
    "ComplianceVerdict",
    "attach_compliance",
    "build_corridor_regulation_map",
    "check_compliance",
]
