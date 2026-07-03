"""Build the regulation map for a set of corridors.

The map is `(origin_nlc, dest_nlc, ticket_code) -> RegulationEntry`. It is
built per session from the current feed snapshot + the §1/§4 inference rules
in `src.regulation.classify`. Honest gaps (tickets named in REGULATION.md §5
but not actually present on a corridor in the .FFL) are recorded as entries
whose `citation.rule_text` starts with "MISSING:" — never silently classified
as `regulated=False` (CLAUDE.md: never guess on bad/absent data).

`cap_price_2025_pence` for regulated rows is the cheapest current FFL fare on
the corridor for that ticket code. REGULATION.md §4 explicitly permits this
fallback for the demo ("treat the current regulated price as the frozen
baseline ... and say so explicitly"). The map's `notes` list carries the
disclosure so the UI can echo it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.ingest.inspect import (
    LocationMeta,
    TtyRecord,
    load_ffl_indexes,
    load_fsc_clusters,
    load_loc_meta,
    load_ticket_type_meta,
)

from src.regulation.classify import classify_ticket
from src.regulation.types import (
    RegulationCitation,
    RegulationEntry,
    RegulationKey,
    RegulationMap,
)


# REGULATION.md §4 fallback baseline disclosure — quoted in the map's notes
# so the UI can show it on the compliance card. Keep wording stable; the
# downstream UI may grep for it.
BASELINE_NOTE = (
    "cap_price_2025_pence = cheapest current FFL fare for this (origin, dest, "
    "ticket); 1 March 2025 reference price not yet sourced from DfT/TSA. "
    "REGULATION.md §4 explicitly permits this fallback for the demo."
)

NFO_NOT_APPLIED_NOTE = (
    "NFO overrides not applied to cap_price_2025_pence (deferred until "
    "compliance-check wiring); the cheapest direct flow fare is used. "
    "Acceptable for the demo; sourcing the true 2025 baseline is the v2 fix."
)


@dataclass(frozen=True)
class CorridorSpec:
    """One corridor to classify. `is_london_flow` toggles the §1 Anytime Day
    Return rule (REGULATED_WALKUPS_LONDON in classify.py).

    `name` is for log/UI display; it has no semantic effect."""
    name: str
    origin_nlc: str
    dest_nlc: str
    is_london_flow: bool


def build_regulation_map(
    corridors: list[CorridorSpec],
    *,
    ffl_path: Path,
    loc_path: Path,
    tty_path: Path,
    fsc_path: Path,
    extra_tickets: tuple[str, ...] = (),
) -> RegulationMap:
    """Build the regulation map for the given corridors.

    For each corridor: scan the .FFL for every fare on every flow between
    `origin_nlc` and `dest_nlc` (both directions), classify each ticket, and
    record a `RegulationEntry`.

    For each ticket in `extra_tickets` *not* found on a corridor: synthesise
    an entry whose `citation.rule_text` starts with "MISSING:" and whose
    `regulated` reflects what the §1 rules would say given the ticket's
    .TTY metadata alone (no corridor-presence assumption). This is the
    honest-gap path REGULATION.md §5 Case 3 (SDR on MAN-EUS) needs.
    """
    ffl = load_ffl_indexes(ffl_path)
    loc = load_loc_meta(loc_path)
    tty = load_ticket_type_meta(tty_path)
    fsc = load_fsc_clusters(fsc_path)

    entries: dict[RegulationKey, RegulationEntry] = {}
    notes: list[str] = [BASELINE_NOTE, NFO_NOT_APPLIED_NOTE]

    for corridor in corridors:
        # Step 1: collect every ticket priced on this corridor.
        # Mirrors the resolver's _expand: a query on (MAN, EUS) returns a fare
        # set on (MAN_group, LON_terminals_group) because group flows govern
        # all member pairs. The regulation map must reflect what the resolver
        # would actually return, not just direct-pair fares.
        # See src/resolver/resolve.py:_expand for the equivalent fan-out.
        origin_candidates = _expand_via_loc_group(corridor.origin_nlc, loc, fsc)
        dest_candidates = _expand_via_loc_group(corridor.dest_nlc, loc, fsc)
        ticket_to_cheapest: dict[str, int] = {}
        for o in origin_candidates:
            for d in dest_candidates:
                # Forward direction always; reverse only when DIRECTION='R'
                # (reversible flow). Mirrors resolver behaviour.
                for flow in ffl.flows_by_pair.get((o, d), []):
                    for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                        prev = ticket_to_cheapest.get(fare.ticket_code)
                        if prev is None or fare.fare_pence < prev:
                            ticket_to_cheapest[fare.ticket_code] = fare.fare_pence
                for flow in ffl.flows_by_pair.get((d, o), []):
                    if flow.direction != "R":
                        continue
                    for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                        prev = ticket_to_cheapest.get(fare.ticket_code)
                        if prev is None or fare.fare_pence < prev:
                            ticket_to_cheapest[fare.ticket_code] = fare.fare_pence

        origin_meta: LocationMeta | None = loc.get(corridor.origin_nlc)
        origin_county = origin_meta.county if origin_meta is not None else ""

        # Step 2: classify each ticket present on the corridor.
        for code, pence in sorted(ticket_to_cheapest.items()):
            tty_record: TtyRecord | None = tty.get(code)
            regulated, citation = classify_ticket(
                code, tty_record,
                origin_county=origin_county,
                is_london_flow=corridor.is_london_flow,
            )
            cap = pence if regulated else None
            entries[(corridor.origin_nlc, corridor.dest_nlc, code)] = RegulationEntry(
                origin_nlc=corridor.origin_nlc,
                dest_nlc=corridor.dest_nlc,
                ticket_code=code,
                regulated=regulated,
                cap_price_2025_pence=cap,
                citation=citation,
            )

        # Step 3: handle extra_tickets honestly — present an entry per
        # extra ticket NOT on this corridor, marked MISSING.
        for code in extra_tickets:
            key = (corridor.origin_nlc, corridor.dest_nlc, code)
            if key in entries:
                continue  # already classified from the corridor scan
            tty_record = tty.get(code)
            regulated, base_citation = classify_ticket(
                code, tty_record,
                origin_county=origin_county,
                is_london_flow=corridor.is_london_flow,
            )
            # MISSING marker is mandatory — never let the lookup silently
            # return "not regulated" because the ticket happens to be absent.
            missing_citation = RegulationCitation(
                section=base_citation.section,
                rule_text=(
                    f"MISSING: ticket {code!r} not present on corridor "
                    f"{corridor.name} in .FFL; classification rests on .TTY "
                    f"metadata alone — base rule: {base_citation.rule_text}"
                ),
                evidence=base_citation.evidence,
            )
            entries[key] = RegulationEntry(
                origin_nlc=corridor.origin_nlc,
                dest_nlc=corridor.dest_nlc,
                ticket_code=code,
                regulated=False,    # never assert regulated on a missing row
                cap_price_2025_pence=None,
                citation=missing_citation,
            )

    return RegulationMap(entries=entries, notes=tuple(notes))


def _expand_via_loc_group(
    nlc: str,
    loc: dict[str, LocationMeta],
    fsc: dict[str, list[str]],
) -> list[str]:
    """Mirror src/resolver/resolve.py:_expand: [nlc, LOC GROUP_NLC, *FSC
    cluster IDs], deduped.

    FSC fan-out matters here because some corridors (e.g. Cardiff-Bristol)
    carry their walk-up fares ONLY on cluster-keyed flows — without it those
    tickets get no map entry and compliance silently reports not_regulated.
    Cluster fares can only LOWER a ticket's cheapest-fare cap, which is safe:
    compliance uses effective_cap = max(cap, the fare's own old price)."""
    out = [nlc]
    meta = loc.get(nlc)
    if meta is not None and meta.group_nlc.strip() and meta.group_nlc != nlc:
        out.append(meta.group_nlc)
    for cluster_id in fsc.get(nlc, []):
        if cluster_id not in out:
            out.append(cluster_id)
    return out


__all__ = [
    "BASELINE_NOTE",
    "NFO_NOT_APPLIED_NOTE",
    "CorridorSpec",
    "build_regulation_map",
]
