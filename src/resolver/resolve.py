"""Deterministic fare resolver — minimal slice with full provenance.

This is the moat. The resolver is pure, deterministic, and side-effect-free.
Provenance is part of the return type from the first line of code, never
bolted on. The LLM never calls into here to compute a price; the LLM only
proposes a *change*, which the resolver re-runs against the staging layer.

This is the THIN slice (CLAUDE.md "resolution order" steps 1-2 only):
    flow record -> fare record -> price + provenance chain

Deferred to later slices (called out where they would slot in):
  - .FSC station-cluster fan-out (so individual MAN/EUS NLCs resolve via group)
  - .NFO non-derivable overrides (highest-precedence in real resolution)
  - Railcard discounting, status discounts, minimum-fare floors
  - .FRR rounding rules
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.ingest.inspect import (
    FlowRecord,
    LocationMeta,
    NfoOverride,
    load_ffl_indexes,
    load_frr_rules,
    load_fsc_clusters,
    load_loc_meta,
    load_nfo_overrides,
    load_railcards,
    load_rcm_min_fares,
    load_status_discounts,
    load_ticket_discount_categories,
)


# --- Provenance types ------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceStep:
    """One link in the chain that produced (or failed to produce) a fare.

    Every step the resolver takes — including null results and disambiguation
    choices — appends one of these. Reading the list end-to-end reconstructs
    exactly why the resolver returned what it did, citing the feed line.
    """
    step: str               # e.g. "flow_lookup", "flow_record", "fare_lookup", "fare_record"
    source: str             # e.g. "data/RJFAF805.FFL line 1247"; "(query)" for lookups
    detail: dict[str, str]  # the parsed fields / query parameters at this step


ResolveStatus = Literal[
    "resolved",         # price found (either via flow fare or override)
    "no_flow",          # no F-record matched the corridor
    "no_fare",          # flow found but no T-record for the ticket
    "ambiguous",        # disambiguation could not pick deterministically
    "suppressed",       # an NFO override marks this fare unavailable (99999999 sentinel)
    "contradiction",    # multiple NFO Y-rows for the same key — never silently guess
]


@dataclass(frozen=True)
class ResolvedFare:
    """The resolver's return type. `provenance` is non-optional by design."""
    origin_nlc: str
    dest_nlc: str
    ticket_code: str
    price_pence: int | None
    status: ResolveStatus
    provenance: list[ProvenanceStep] = field(default_factory=list)


# --- Boundary validation ---------------------------------------------------


def _validate_inputs(origin_nlc: str, dest_nlc: str, ticket_code: str) -> None:
    """Reject obviously malformed inputs at the boundary. Internal callers are
    trusted; this only guards the public entry point."""
    if not (len(origin_nlc) == 4 and origin_nlc.isalnum()):
        raise ValueError(f"origin_nlc must be 4 alnum chars, got {origin_nlc!r}")
    if not (len(dest_nlc) == 4 and dest_nlc.isalnum()):
        raise ValueError(f"dest_nlc must be 4 alnum chars, got {dest_nlc!r}")
    if not (len(ticket_code) == 3 and ticket_code.isalnum()):
        raise ValueError(f"ticket_code must be 3 alnum chars, got {ticket_code!r}")


# --- The resolver ----------------------------------------------------------


def resolve_fare(
    origin_nlc: str,
    dest_nlc: str,
    ticket_code: str,
    feed_path: Path,
    *,
    loc_path: Path | None = None,
    fsc_path: Path | None = None,
    nfo_path: Path | None = None,
    rlc_path: Path | None = None,
    dis_path: Path | None = None,
    rcm_path: Path | None = None,
    frr_path: Path | None = None,
    tty_path: Path | None = None,
    route_code: str | None = None,
    railcard_code: str | None = None,
) -> ResolvedFare:
    """Resolve one fare on one corridor for one ticket code.

    Algorithm:
      1. If `loc_path` is given, look up GROUP_NLC for origin and dest; expand
         each NLC into [self, group] so we also try the group-level flow
         when no station-specific flow exists. (Blast-radius foundation:
         flows set on the group apply to every member station.)
      2. Find F-records matching ANY of the expanded (origin, dest) pairs
         in a single .FFL scan.
      3. If `route_code` is given, filter flows to that route. Otherwise prefer
         route '00000' (ANY PERMITTED) when present; else fall back to
         lowest FLOW_ID deterministically.
      4. Under the chosen FLOW_ID, find the T-record whose TICKET_CODE matches.
      5. Return price + the full provenance chain.

    Null results (no flow / no fare) return a ResolvedFare with
    price_pence=None and status set; the provenance always explains the miss.
    """
    _validate_inputs(origin_nlc, dest_nlc, ticket_code)
    if route_code is not None and not (len(route_code) == 5 and route_code.isalnum()):
        raise ValueError(f"route_code must be 5 alnum chars, got {route_code!r}")
    feed_path = Path(feed_path)
    feed_label = feed_path.name

    prov: list[ProvenanceStep] = [
        ProvenanceStep(
            step="flow_lookup",
            source="(query)",
            detail={
                "origin_nlc": origin_nlc,
                "dest_nlc": dest_nlc,
                "ticket_code": ticket_code,
                "route_code": route_code or "(any)",
                "feed": str(feed_path),
            },
        )
    ]

    # --- Cluster fan-out via LOC GROUP_NLC and FSC member->cluster index ---
    loc_meta: dict[str, LocationMeta] | None = None
    if loc_path is not None:
        loc_meta = load_loc_meta(Path(loc_path))
    fsc_clusters: dict[str, list[str]] | None = None
    if fsc_path is not None:
        fsc_clusters = load_fsc_clusters(Path(fsc_path))

    origin_candidates = _expand(origin_nlc, loc_meta, fsc_clusters, prov, "origin", loc_path, fsc_path)
    dest_candidates   = _expand(dest_nlc,   loc_meta, fsc_clusters, prov, "dest",   loc_path, fsc_path)

    pairs: list[tuple[str, str]] = []
    for o in origin_candidates:
        for d in dest_candidates:
            pairs.append((o, d))

    # Cached index lookup — replaces a full FFL scan per query.
    indexes = load_ffl_indexes(feed_path)
    matches: dict[tuple[str, str], list[FlowRecord]] = {
        p: indexes.flows_by_pair.get(p, []) for p in pairs
    }
    all_flows: list[FlowRecord] = [f for fs in matches.values() for f in fs]

    if not all_flows:
        prov.append(ProvenanceStep(
            step="flow_lookup_result",
            source="(query)",
            detail={
                "found": "0",
                "pairs_tried": ";".join(f"{o}->{d}" for o, d in pairs),
                "explanation": (
                    f"no F-record matched any of {len(pairs)} (origin,dest) pairs after "
                    "group expansion; .FSC small-cluster fan-out and DIRECTION='R' swap not yet wired"
                ),
            },
        ))
        return ResolvedFare(
            origin_nlc=origin_nlc,
            dest_nlc=dest_nlc,
            ticket_code=ticket_code,
            price_pence=None,
            status="no_flow",
            provenance=prov,
        )

    # Record which (o,d) pair won — useful when group-level resolves but station-level didn't.
    winning_pair = next(((o, d) for (o, d), fs in matches.items() if fs), None)
    if winning_pair and (winning_pair != (origin_nlc, dest_nlc)):
        prov.append(ProvenanceStep(
            step="group_match",
            source=f"{feed_label} (multi-pair scan)",
            detail={
                "queried_pair":  f"{origin_nlc}->{dest_nlc}",
                "matched_pair":  f"{winning_pair[0]}->{winning_pair[1]}",
                "explanation":   "no station-pair flow; matched at group level via LOC GROUP_NLC fan-out",
            },
        ))

    chosen = _pick_flow(all_flows, prov, feed_label, route_code=route_code)

    prov.append(ProvenanceStep(
        step="flow_record",
        source=f"{feed_label} line {chosen.line_no}",
        detail={
            "ORIGIN_CODE":   chosen.origin_nlc,
            "DESTINATION_CODE": chosen.dest_nlc,
            "ROUTE_CODE":    chosen.route_code,
            "STATUS_CODE":   chosen.status_code,
            "USAGE_CODE":    chosen.usage_code,
            "DIRECTION":     chosen.direction,
            "TOC":           chosen.toc,
            "FLOW_ID":       chosen.flow_id,
        },
    ))

    # --- NFO override layer (highest precedence per RSPS5045 §4.13) --------
    # CLAUDE.md rules honoured: COMPOSITE='Y' only (loader filters); ADULT_FARE
    # 99999999 = suppression (fare unavailable, NOT £999,999); multiple matching
    # rows = contradiction (we escalate, never silently guess).
    if nfo_path is not None:
        nfo_index = load_nfo_overrides(Path(nfo_path))
        override_result = _apply_nfo_override(
            nfo_index, chosen, ticket_code, railcard_code or "   ",
            prov, Path(nfo_path).name,
        )
        if override_result is not None:
            # Override fully decides the answer: apply, suppress, or contradict.
            return ResolvedFare(
                origin_nlc=origin_nlc,
                dest_nlc=dest_nlc,
                ticket_code=ticket_code,
                price_pence=override_result[0],
                status=override_result[1],
                provenance=prov,
            )

    fares = indexes.fares_by_flow.get(chosen.flow_id, [])
    prov.append(ProvenanceStep(
        step="fare_lookup",
        source="(query)",
        detail={
            "flow_id": chosen.flow_id,
            "ticket_code": ticket_code,
            "total_fares_on_flow": str(len(fares)),
        },
    ))

    match = next((f for f in fares if f.ticket_code == ticket_code), None)
    if match is None:
        prov.append(ProvenanceStep(
            step="fare_lookup_result",
            source="(query)",
            detail={
                "found": "0",
                "available_tickets": ",".join(sorted(f.ticket_code for f in fares)) or "(none)",
                "explanation": f"ticket_code={ticket_code} not present on FLOW_ID={chosen.flow_id}",
            },
        ))
        return ResolvedFare(
            origin_nlc=origin_nlc,
            dest_nlc=dest_nlc,
            ticket_code=ticket_code,
            price_pence=None,
            status="no_fare",
            provenance=prov,
        )

    prov.append(ProvenanceStep(
        step="fare_record",
        source=f"{feed_label} line {match.line_no}",
        detail={
            "FLOW_ID":          match.flow_id,
            "TICKET_CODE":      match.ticket_code,
            "FARE_pence":       str(match.fare_pence),
            "RESTRICTION_CODE": match.restriction_code or "(none)",
        },
    ))

    # --- Railcard discount chain (.RLC -> .TTY -> .DIS -> .RCM -> .FRR) ---
    # Fully feed-derived via src.resolver.railcard; every step appends its
    # own ProvenanceStep citing the exact feed line read. A railcard query
    # without the four feed paths is a programmer error — quarantine rather
    # than silently treat it as an adult query.
    final_pence: int = match.fare_pence
    if railcard_code:
        missing = [
            name for name, p in (("rlc", rlc_path), ("dis", dis_path),
                                 ("rcm", rcm_path), ("frr", frr_path),
                                 ("tty", tty_path))
            if p is None
        ]
        if missing:
            prov.append(ProvenanceStep(
                step="railcard_unwired",
                source="(resolver)",
                detail={
                    "railcard_code": railcard_code,
                    "missing_paths": ",".join(missing),
                    "explanation":   (
                        "railcard requested but feed paths not supplied; pass "
                        "rlc_path/dis_path/rcm_path/frr_path/tty_path to resolve_fare()"
                    ),
                },
            ))
            return ResolvedFare(
                origin_nlc=origin_nlc, dest_nlc=dest_nlc, ticket_code=ticket_code,
                price_pence=None, status="no_fare", provenance=prov,
            )
        # Local import keeps the railcard module's dependency on ProvenanceStep
        # from forming an import cycle. The `missing` guard above proves
        # every path is non-None; assert satisfies the type checker too.
        from src.resolver.railcard import apply_railcard_from_feed
        assert rlc_path is not None
        assert dis_path is not None
        assert rcm_path is not None
        assert frr_path is not None
        assert tty_path is not None
        rlc = Path(rlc_path)
        dis = Path(dis_path)
        rcm = Path(rcm_path)
        frr = Path(frr_path)
        tty = Path(tty_path)
        outcome = apply_railcard_from_feed(
            base_pence=final_pence,
            railcard_code=railcard_code,
            ticket_code=ticket_code,
            railcards=load_railcards(rlc),
            status_discounts=load_status_discounts(dis),
            rcm_min_fares=load_rcm_min_fares(rcm),
            frr_rules=load_frr_rules(frr),
            ticket_categories=load_ticket_discount_categories(tty),
            rlc_label=rlc.name, dis_label=dis.name, rcm_label=rcm.name,
            frr_label=frr.name, tty_label=tty.name,
        )
        prov.extend(outcome.provenance)
        if outcome.price_pence is None:
            return ResolvedFare(
                origin_nlc=origin_nlc, dest_nlc=dest_nlc, ticket_code=ticket_code,
                price_pence=None, status="no_fare", provenance=prov,
            )
        final_pence = outcome.price_pence

    return ResolvedFare(
        origin_nlc=origin_nlc,
        dest_nlc=dest_nlc,
        ticket_code=ticket_code,
        price_pence=final_pence,
        status="resolved",
        provenance=prov,
    )


_ANY_PERMITTED_ROUTE = "00000"
_NFO_ANY_ROUTE_WILDCARDS = {"*****", "     ", ""}
_NFO_NO_RAILCARD = "   "  # blank-padded; means "adult fare, no railcard"


def _apply_nfo_override(
    nfo_index: dict[tuple[str, str, str, str, str], list[NfoOverride]],
    chosen: FlowRecord,
    ticket_code: str,
    railcard_code: str,
    prov: list[ProvenanceStep],
    nfo_label: str,
) -> tuple[int | None, ResolveStatus] | None:
    """Look up an NFO override for the chosen flow + ticket + railcard.
    Returns (price, status) when an override decides the answer; None means
    'no override matched — fall through to the flow fare as usual'.

    Match order — exact origin/dest/ticket/railcard, then route-wildcard:
      1. (origin, dest, chosen.route, railcard, ticket) — exact route
      2. (origin, dest, '*****' or '     ' or '', railcard, ticket) — any-route
    """
    o, d, route, rlc, tkt = (
        chosen.origin_nlc, chosen.dest_nlc, chosen.route_code, railcard_code, ticket_code,
    )

    hits: list[NfoOverride] = list(nfo_index.get((o, d, route, rlc, tkt), []))
    # Add any-route wildcard rows under the same other-fields key.
    for wildcard in _NFO_ANY_ROUTE_WILDCARDS:
        hits.extend(nfo_index.get((o, d, wildcard, rlc, tkt), []))

    if not hits:
        return None  # no override; resolver continues with the flow fare

    if len(hits) > 1:
        # CLAUDE.md: never silently guess on contradictions.
        prov.append(ProvenanceStep(
            step="override_contradiction",
            source=f"{nfo_label} lines {','.join(str(h.line_no) for h in hits)}",
            detail={
                "key":           f"{o}->{d} route={route} rlc={rlc!r} ticket={tkt}",
                "candidate_fares": ";".join(
                    f"L{h.line_no}={'SUPPRESSED' if h.is_suppression else h.adult_fare_pence}"
                    for h in hits
                ),
                "explanation":   "multiple NFO Y-records match the same key; escalating instead of picking",
            },
        ))
        return (None, "contradiction")

    override = hits[0]
    if override.is_suppression:
        prov.append(ProvenanceStep(
            step="override_suppression",
            source=f"{nfo_label} line {override.line_no}",
            detail={
                "key":          f"{o}->{d} route={route} rlc={rlc!r} ticket={tkt}",
                "sentinel":     "ADULT_FARE=99999999",
                "explanation":  "NFO override marks this fare as unavailable (suppression sentinel, NOT £999,999)",
            },
        ))
        return (None, "suppressed")

    prov.append(ProvenanceStep(
        step="override_applied",
        source=f"{nfo_label} line {override.line_no}",
        detail={
            "key":               f"{o}->{d} route={route} rlc={rlc!r} ticket={tkt}",
            "override_pence":    str(override.adult_fare_pence),
            "matched_route":     override.route_code or "(any)",
            "explanation":       "NFO override takes precedence over the flow fare",
        },
    ))
    return (override.adult_fare_pence, "resolved")


def _expand(
    nlc: str,
    loc_meta: dict[str, LocationMeta] | None,
    fsc_clusters: dict[str, list[str]] | None,
    prov: list[ProvenanceStep],
    role: str,                          # "origin" or "dest"
    loc_path: Path | None,
    fsc_path: Path | None,
) -> list[str]:
    """Expand `nlc` to [nlc, LOC_group, *FSC_clusters] for blast-radius fan-out.

    Two independent expansion sources, both append their own provenance step:
      - LOC GROUP_NLC: regional groups (e.g. EUS 1444 -> LON TERMINALS 1072).
      - FSC clusters:  TOC-specific groups (e.g. MAN_PICC 2968 is a member of
        LUMO's destination cluster LS57, which fares like LUMO route 01491 use).

    Returns a deduped list preserving the discovery order (self first).
    """
    out: list[str] = [nlc]

    if loc_meta is not None:
        meta = loc_meta.get(nlc)
        if meta is not None and meta.group_nlc != nlc and meta.group_nlc.strip():
            prov.append(ProvenanceStep(
                step="group_expansion",
                source=f"{Path(loc_path).name if loc_path else 'LOC'} line {meta.line_no}",
                detail={
                    "role":           role,
                    "station_nlc":    nlc,
                    "station_name":   meta.station_name,
                    "crs":            meta.crs or "(none)",
                    "group_nlc":      meta.group_nlc,
                    "explanation":    f"{role} {nlc} ({meta.station_name}) is a member of LOC group {meta.group_nlc}",
                },
            ))
            if meta.group_nlc not in out:
                out.append(meta.group_nlc)

    if fsc_clusters is not None:
        clusters = fsc_clusters.get(nlc, [])
        if clusters:
            new = [c for c in clusters if c not in out]
            if new:
                prov.append(ProvenanceStep(
                    step="cluster_expansion",
                    source=f"{Path(fsc_path).name if fsc_path else 'FSC'} (member->cluster index)",
                    detail={
                        "role":           role,
                        "station_nlc":    nlc,
                        "fsc_clusters":   ",".join(new),
                        "explanation":    f"{role} {nlc} is a member of {len(new)} .FSC cluster(s); fares set on these cluster IDs apply",
                    },
                ))
                out.extend(new)

    return out


def _pick_flow(
    flows: list[FlowRecord],
    prov: list[ProvenanceStep],
    feed_label: str,
    *,
    route_code: str | None = None,
) -> FlowRecord:
    """Pick one flow when several match. Rule order:
      1. If `route_code` is given, require exact match; if none match, fall
         through to the rest (and the provenance flags the miss).
      2. Otherwise prefer ROUTE='00000' (ANY PERMITTED) when available — that's
         the BRFares default and the safest pick for an unspecified query.
      3. Fall back to lowest FLOW_ID deterministically.

    All branches record the chosen rule and the candidates in provenance.
    """
    if len(flows) == 1:
        return flows[0]

    candidates = sorted(flows, key=lambda f: (f.flow_id))
    cand_summary = ";".join(
        f"FLOW_ID={f.flow_id}/ROUTE={f.route_code}/TOC={f.toc}/DIR={f.direction}"
        for f in candidates
    )

    if route_code is not None:
        route_matches = [f for f in candidates if f.route_code == route_code]
        if route_matches:
            chosen = route_matches[0]
            prov.append(ProvenanceStep(
                step="flow_disambiguation",
                source=f"{feed_label} lines {','.join(str(f.line_no) for f in candidates)}",
                detail={
                    "candidates":     cand_summary,
                    "chosen_flow_id": chosen.flow_id,
                    "rule":           f"explicit route_code={route_code} requested; matched exactly",
                },
            ))
            return chosen
        # Requested route not present — fall through to default but record the miss.
        prov.append(ProvenanceStep(
            step="route_request_unmet",
            source=f"{feed_label} (disambiguation)",
            detail={
                "requested_route": route_code,
                "available_routes": ",".join(sorted({f.route_code for f in candidates})),
                "explanation":     "requested route_code not in candidates; falling back to default disambiguation",
            },
        ))

    any_permitted = [f for f in candidates if f.route_code == _ANY_PERMITTED_ROUTE]
    if any_permitted:
        chosen = sorted(any_permitted, key=lambda f: f.flow_id)[0]
        prov.append(ProvenanceStep(
            step="flow_disambiguation",
            source=f"{feed_label} lines {','.join(str(f.line_no) for f in candidates)}",
            detail={
                "candidates":     cand_summary,
                "chosen_flow_id": chosen.flow_id,
                "rule":           "prefer ROUTE='00000' (ANY PERMITTED) when no explicit route was requested",
            },
        ))
        return chosen

    chosen = candidates[0]
    prov.append(ProvenanceStep(
        step="flow_disambiguation",
        source=f"{feed_label} lines {','.join(str(f.line_no) for f in candidates)}",
        detail={
            "candidates":     cand_summary,
            "chosen_flow_id": chosen.flow_id,
            "rule":           "no '00000' route present; fell back to lowest FLOW_ID",
        },
    ))
    return chosen


