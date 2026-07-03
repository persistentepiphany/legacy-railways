"""Compute the affected set for a ChangeRequest.

Two distinct outputs, never silently summed across the wrong axis:

  canonical_affected
      One AffectedFare per (flow_id, ticket_code) actually repriced.
      THIS is what revenue exposure sums over.

  blast_radius_pairs
      Every (origin_nlc, dest_nlc, canonical_idx) reachable through
      cluster fan-out. THIS is what the GB-map shows.

Cluster fan-out uses LOC GROUP_NLC (the same mechanism the resolver uses in
src/resolver/resolve.py:_expand). A flow set on a group NLC governs every
(member_origin, member_dest) station pair — that's the blast-radius source.

We do NOT call `resolve_fare` per canonical row. The resolver picks ONE flow
per (o,d) query via disambiguation; the impact engine needs the full set of
flows touched by the change. So we walk `FFLIndexes.flows_by_pair` directly
and produce one AffectedFare per (flow_id, ticket_code) in the corridor's
cluster cross-product. Determinism: output is sorted by (flow_id, ticket_code)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from src.ingest.inspect import (
    FFLIndexes,
    LocationMeta,
    TtyRecord,
    load_ffl_indexes,
    load_fsc_clusters,
    load_loc_meta,
    load_ticket_type_meta,
)
from src.resolver.resolve import ProvenanceStep, ResolveStatus

from src.impact.change_request import ChangeRequest
from src.impact.feed_paths import FeedPaths
from src.impact.synthetic_railcard import apply_synthetic_railcard

if TYPE_CHECKING:
    # Forward reference only — avoids a circular import. compliance.py imports
    # AffectedFare/AffectedSet from this module to do its join; we only need
    # the type name here for annotations (deferred by `from __future__ import
    # annotations`).
    from src.impact.compliance import ComplianceVerdict


@dataclass(frozen=True)
class AffectedFare:
    """One canonical row repriced by the change."""
    flow_id: str
    ticket_code: str
    route_code: str                    # 5-char; '00000' = any-permitted
    representative_origin_nlc: str     # the (o,d) used to read this flow
    representative_dest_nlc: str
    status: ResolveStatus              # 'resolved' for the bulk path; reserved for future-injected
    old_price_pence: int | None        # None only if status != 'resolved'
    new_price_pence: int | None        # None only if status != 'resolved'
    discount_category: str             # 2-char .TTY DISCOUNT_CATEGORY
    provenance: tuple[ProvenanceStep, ...]
    blast_radius_pairs: tuple[tuple[str, str], ...]  # all (o,d) governed by this flow_id
    # Populated by src.impact.compliance.attach_compliance after the row is
    # built. Default None so callers of compute_affected_set that don't want
    # the compliance join (e.g. unit tests) get a still-valid row.
    compliance: "ComplianceVerdict | None" = None
    # Human-readable .LOC names for the representative pair (group NLCs like
    # 0438 have LOC rows too, so "0438 → 1072" becomes "MANCHESTER GRP → LONDON GRP").
    representative_origin_name: str = ""
    representative_dest_name: str = ""
    # Every individual station NLC touched by this fare's blast radius:
    # both sides of blast_radius_pairs expanded through LOC group membership,
    # deduped, sorted, capped. This is what the GB map lights up.
    blast_station_nlcs: tuple[str, ...] = ()


ExpansionReason: TypeAlias = Literal[
    "direct",
    "loc_group_origin", "loc_group_dest", "loc_group_both",
    "fsc_cluster_origin", "fsc_cluster_dest", "fsc_cluster_both",
]


@dataclass(frozen=True)
class BlastRadiusPair:
    """One (origin, dest) reachable through cluster fan-out, with a back-link
    to the canonical row it inherits."""
    origin_nlc: str
    dest_nlc: str
    canonical_index: int
    expansion_reason: ExpansionReason


@dataclass(frozen=True)
class ScopeStats:
    """Honest bookkeeping for the affected set's scale, before/after any
    truncation. At operator (TOC) scope the full set can be tens of
    thousands of rows; aggregates run over ALL of them but only the top-N
    detailed rows survive into the report — these counters say exactly
    how much was cut (never silently)."""
    scope: Literal["corridor", "toc"]
    toc_code: str | None
    flows_total: int
    flows_actual: int              # usage_code='A'
    flows_generated_skipped: int   # usage_code='G' excluded at TOC scope
    canonical_total: int
    canonical_returned: int
    blast_pairs_total: int
    blast_pairs_returned: int
    truncated: bool
    # Deduped union of every station NLC in the scope's network (capped);
    # the GB map lights these up for an operator-scoped change.
    toc_station_nlcs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AffectedSet:
    """The result of compute_affected_set."""
    canonical: tuple[AffectedFare, ...]
    skipped: tuple[AffectedFare, ...]
    blast_radius: tuple[BlastRadiusPair, ...]
    notes: tuple[str, ...]
    stats: ScopeStats | None = None


def _entity_members(
    loc: dict[str, LocationMeta],
    fsc: dict[str, list[str]],
) -> dict[str, set[str]]:
    """Reverse maps: expansion entity → member stations it governs.
      LOC: GROUP_NLC → members (who 0438 contains)
      FSC: CLUSTER_ID → members (who Q496 contains)
    A leaf NLC (e.g. 2968) has no entry; callers fall back to {nlc}."""
    out: dict[str, set[str]] = defaultdict(set)
    for nlc, meta in loc.items():
        if meta.group_nlc.strip():
            out[meta.group_nlc].add(nlc)
    for member_nlc, cluster_ids in fsc.items():
        for cluster_id in cluster_ids:
            out[cluster_id].add(member_nlc)
    return out


def compute_affected_set(change: ChangeRequest, feed_paths: FeedPaths) -> AffectedSet:
    """Walk the change's scope; produce canonical rows + blast-radius pairs.
    Pure-ish: reads the feed via mtime-cached loaders. Corridor scope walks
    the cluster cross-product below; TOC scope walks the operator's flows."""
    if change.scope == "toc":
        return _compute_affected_set_toc(change, feed_paths)
    return _compute_affected_set_corridor(change, feed_paths)


def _compute_affected_set_corridor(change: ChangeRequest, feed_paths: FeedPaths) -> AffectedSet:
    """Walk the corridor's cluster cross-product; produce canonical rows +
    blast-radius pairs. Pure-ish: reads the feed via mtime-cached loaders.

    Algorithm:
      1. Expand origin/dest to [self, LOC group, *FSC clusters] — the same
         fan-out the resolver does in src/resolver/resolve.py:_expand.
      2. Walk `flows_by_pair` for the cross-product, both directions (with
         the DIRECTION='R' filter on the reverse leg).
      3. Filter fares by .TTY DISCOUNT_CATEGORY (the change's scope).
      4. Group hits by (flow_id, ticket_code) → one AffectedFare per group.
      5. For each AffectedFare, derive its blast_radius_pairs by expanding
         the flow's (origin, dest) into member NLCs (LOC group / FSC
         cluster reverse lookup).
    """
    ffl = load_ffl_indexes(feed_paths.ffl)
    loc = load_loc_meta(feed_paths.loc)
    tty = load_ticket_type_meta(feed_paths.tty)
    fsc = load_fsc_clusters(feed_paths.fsc)  # MEMBER_NLC → [CLUSTER_ID, ...]

    notes: list[str] = []
    scope = set(change.discount_categories)

    entity_to_members = _entity_members(loc, fsc)

    # Per-side expansion kind: how each candidate NLC was reached. Feeds the
    # blast pair's expansion_reason so the GB map can say WHY a pair lit up.
    origin_expansion, origin_kind = _expand_via_loc_and_fsc(
        change.corridor_origin_nlc, loc, fsc)
    dest_expansion, dest_kind = _expand_via_loc_and_fsc(
        change.corridor_dest_nlc, loc, fsc)

    @dataclass
    class _Accum:
        """Aggregator for one (flow_id, ticket_code) before becoming an AffectedFare."""
        flow_id: str
        ticket_code: str
        route_code: str
        ffl_old_pence: int
        rep_origin: str
        rep_dest: str
        flow_origin_code: str   # the flow record's stored O/D — a leaf NLC,
        flow_dest_code: str     # a LOC group NLC, or an FSC cluster ID
        fare_line_no: int       # .FFL T-record line — provenance raw_record
        flow_line_no: int       # .FFL F-record line
        # (member_o, member_d) → (origin_kind, dest_kind); kinds merged with
        # _KIND_RANK priority so a pair re-derived through a wider fan-out
        # keeps its most direct explanation.
        blast_pairs: dict[tuple[str, str], tuple[str, str]]

    accum: dict[tuple[str, str], _Accum] = {}

    def _add_pairs(rec: _Accum, fan_origin: str, fan_dest: str,
                   o_kind: str, d_kind: str) -> None:
        """Fan (fan_origin, fan_dest) out to member pairs, tagging each with
        the per-side expansion kind. Leaf NLCs fall back to themselves."""
        o_members = entity_to_members.get(fan_origin) or {fan_origin}
        d_members = entity_to_members.get(fan_dest) or {fan_dest}
        for mo in o_members:
            for md in d_members:
                prev = rec.blast_pairs.get((mo, md))
                if prev is None:
                    rec.blast_pairs[(mo, md)] = (o_kind, d_kind)
                else:
                    rec.blast_pairs[(mo, md)] = (
                        min(prev[0], o_kind, key=_KIND_RANK.__getitem__),
                        min(prev[1], d_kind, key=_KIND_RANK.__getitem__),
                    )

    def _consume_flow(o: str, d: str, flow_origin: str, flow_dest: str, ffl_index: FFLIndexes) -> None:
        """Walk fares on this (o,d) flow, accumulating into canonical rows."""
        for flow in ffl_index.flows_by_pair.get((flow_origin, flow_dest), []):
            for fare in ffl_index.fares_by_flow.get(flow.flow_id, []):
                tty_rec: TtyRecord | None = tty.get(fare.ticket_code)
                if tty_rec is None:
                    continue
                if tty_rec.discount_category not in scope:
                    continue
                key = (flow.flow_id, fare.ticket_code)
                rec = accum.get(key)
                if rec is None:
                    rec = _Accum(
                        flow_id=flow.flow_id,
                        ticket_code=fare.ticket_code,
                        route_code=flow.route_code,
                        ffl_old_pence=fare.fare_pence,
                        rep_origin=o, rep_dest=d,
                        flow_origin_code=flow_origin,
                        flow_dest_code=flow_dest,
                        fare_line_no=fare.line_no,
                        flow_line_no=flow.line_no,
                        blast_pairs={},
                    )
                    accum[key] = rec
                # Blast radius: the flow's (flow_origin, flow_dest) governs
                # every (member_o, member_d) station pair via .LOC group /
                # .FSC cluster membership.
                _add_pairs(rec, flow_origin, flow_dest,
                           origin_kind[flow_origin], dest_kind[flow_dest])

    for o in origin_expansion:
        for d in dest_expansion:
            _consume_flow(o, d, o, d, ffl)
            # Reverse-leg flows only when DIRECTION='R' — otherwise they
            # represent fares for the opposite demand direction (different fares).
            for flow in ffl.flows_by_pair.get((d, o), []):
                if flow.direction != "R":
                    continue
                # Walk this flow's fares as if it were a forward (o,d) flow.
                for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                    tty_rec = tty.get(fare.ticket_code)
                    if tty_rec is None or tty_rec.discount_category not in scope:
                        continue
                    key = (flow.flow_id, fare.ticket_code)
                    rec = accum.get(key)
                    if rec is None:
                        rec = _Accum(
                            flow_id=flow.flow_id,
                            ticket_code=fare.ticket_code,
                            route_code=flow.route_code,
                            ffl_old_pence=fare.fare_pence,
                            rep_origin=o, rep_dest=d,
                            # The flow record is stored in its native (d, o)
                            # orientation — cite what's actually in the .FFL.
                            flow_origin_code=d,
                            flow_dest_code=o,
                            fare_line_no=fare.line_no,
                            flow_line_no=flow.line_no,
                            blast_pairs={},
                        )
                        accum[key] = rec
                    # Reverse-R flows are stored (d, o) but the row reports
                    # rep_origin=o, rep_dest=d — emit blast pairs in that same
                    # customer-facing demand direction (o, d).
                    _add_pairs(rec, o, d, origin_kind[o], dest_kind[d])

    if not accum:
        notes.append(
            f"no fares matched corridor "
            f"({change.corridor_origin_nlc}->{change.corridor_dest_nlc}) "
            f"× discount_categories={list(change.discount_categories)}; "
            "ChangeRequest is a no-op against this feed snapshot"
        )

    # Apply synthetic discount, build AffectedFare rows, sort deterministically.
    canonical: list[AffectedFare] = []
    blast_pairs_out: list[BlastRadiusPair] = []
    for key in sorted(accum.keys()):
        rec = accum[key]
        new_pence, synth_step = apply_synthetic_railcard(rec.ffl_old_pence, change)
        provenance: tuple[ProvenanceStep, ...] = (
            ProvenanceStep(
                step="affected_set_pick",
                source=f"{feed_paths.ffl.name} flow_id={rec.flow_id}",
                detail={
                    "ticket_code":       rec.ticket_code,
                    "route_code":        rec.route_code,
                    "representative":    f"{rec.rep_origin}->{rec.rep_dest}",
                    "flow_origin_code":  rec.flow_origin_code,
                    "flow_dest_code":    rec.flow_dest_code,
                    "blast_pairs_count": str(len(rec.blast_pairs)),
                    "fare_line_no":      str(rec.fare_line_no),
                    "flow_line_no":      str(rec.flow_line_no),
                    "explanation":       (
                        "flow_id selected from FFLIndexes.flows_by_pair after "
                        "LOC group + FSC cluster fan-out; "
                        ".TTY DISCOUNT_CATEGORY in scope"
                    ),
                },
            ),
            synth_step,
        )
        idx = len(canonical)
        tty_rec = tty.get(rec.ticket_code)
        assert tty_rec is not None  # filtered above
        blast_stations: set[str] = set()
        for (mo, md) in rec.blast_pairs:
            # Pairs from the reverse-R branch can still carry group/cluster
            # NLCs; expand each side to member stations (leaves pass through).
            blast_stations.update(entity_to_members.get(mo) or {mo})
            blast_stations.update(entity_to_members.get(md) or {md})
        canonical.append(AffectedFare(
            flow_id=rec.flow_id,
            ticket_code=rec.ticket_code,
            route_code=rec.route_code,
            representative_origin_nlc=rec.rep_origin,
            representative_dest_nlc=rec.rep_dest,
            status="resolved",
            old_price_pence=rec.ffl_old_pence,
            new_price_pence=new_pence,
            discount_category=tty_rec.discount_category,
            provenance=provenance,
            blast_radius_pairs=tuple(sorted(rec.blast_pairs)),
            representative_origin_name=_loc_name(rec.rep_origin, loc),
            representative_dest_name=_loc_name(rec.rep_dest, loc),
            blast_station_nlcs=tuple(sorted(blast_stations)[:200]),
        ))
        for (mo, md) in sorted(rec.blast_pairs):
            o_kind, d_kind = rec.blast_pairs[(mo, md)]
            blast_pairs_out.append(BlastRadiusPair(
                origin_nlc=mo, dest_nlc=md,
                canonical_index=idx,
                expansion_reason=_reason_from_kinds(o_kind, d_kind),
            ))

    # Stable, deterministic ordering by (origin, dest, canonical_index).
    blast_pairs_out.sort(key=lambda p: (p.origin_nlc, p.dest_nlc, p.canonical_index))

    flow_ids = {r.flow_id for r in canonical}
    return AffectedSet(
        canonical=tuple(canonical),
        skipped=tuple(),  # bulk path has no skips (every fare has an int price)
        blast_radius=tuple(blast_pairs_out),
        notes=tuple(notes),
        stats=ScopeStats(
            scope="corridor",
            toc_code=None,
            flows_total=len(flow_ids),
            flows_actual=len(flow_ids),
            flows_generated_skipped=0,
            canonical_total=len(canonical),
            canonical_returned=len(canonical),
            blast_pairs_total=len(blast_pairs_out),
            blast_pairs_returned=len(blast_pairs_out),
            truncated=False,
        ),
    )


# Bounding at operator scope. Blast pairs would be millions uncapped (GWR
# has 164k flows); the map only needs the network union + a bounded pair
# sample. Every cut is counted in ScopeStats and noted — never silent.
_TOC_BLAST_PAIR_CAP = 5_000    # total BlastRadiusPairs emitted per report
_TOC_ROW_PAIR_CAP = 512        # per canonical row
_TOC_STATION_CAP = 2_500       # network union (GB has ~2,570 stations)


def _expansion_kind(
    token: str,
    entity_to_members: dict[str, set[str]],
    loc: dict[str, LocationMeta],
) -> str:
    """How a flow's stored O/D token fans out: a leaf NLC is 'direct'; a
    token with members is a LOC group if .LOC knows it, else an FSC cluster
    ID (cluster IDs like Q496 have no .LOC row)."""
    if token not in entity_to_members:
        return "direct"
    return "loc_group" if token in loc else "fsc_cluster"


def _compute_affected_set_toc(change: ChangeRequest, feed_paths: FeedPaths) -> AffectedSet:
    """Operator (TOC) scope: every fare on every usage_code='A' flow of one
    fare-TOC code. No corridor cross-product search — the operator's flow
    list IS the scope; cluster fan-out still applies where a flow's O/D is
    a LOC group or FSC cluster.

    Scale honesty: the FULL canonical set is returned (downstream aggregates
    sum over all of it; the report truncates detailed rows afterwards and
    records the cut in ScopeStats). Blast pairs are capped HERE, in
    deterministic sorted order, with the uncut total counted."""
    ffl = load_ffl_indexes(feed_paths.ffl)
    loc = load_loc_meta(feed_paths.loc)
    tty = load_ticket_type_meta(feed_paths.tty)
    fsc = load_fsc_clusters(feed_paths.fsc)
    entity_to_members = _entity_members(loc, fsc)

    notes: list[str] = []
    scope = set(change.discount_categories)
    toc_code = change.toc_code or ""
    all_flows = ffl.flows_by_toc.get(toc_code, [])
    actual = [f for f in all_flows if f.usage_code == "A"]
    skipped_g = len(all_flows) - len(actual)
    if skipped_g:
        notes.append(
            f"operator scope {toc_code}: {skipped_g} generated (usage_code='G') "
            "flows excluded; only actual ('A') flows are repriced"
        )

    @dataclass
    class _TocRow:
        flow_id: str
        ticket_code: str
        route_code: str
        origin_nlc: str
        dest_nlc: str
        old_pence: int
        discount_category: str
        fare_line_no: int
        flow_line_no: int

    rows: dict[tuple[str, str], _TocRow] = {}
    station_union: set[str] = set()
    for flow in actual:
        contributed = False
        for fare in ffl.fares_by_flow.get(flow.flow_id, []):
            tty_rec: TtyRecord | None = tty.get(fare.ticket_code)
            if tty_rec is None or tty_rec.discount_category not in scope:
                continue
            key = (flow.flow_id, fare.ticket_code)
            if key not in rows:
                rows[key] = _TocRow(
                    flow_id=flow.flow_id,
                    ticket_code=fare.ticket_code,
                    route_code=flow.route_code,
                    origin_nlc=flow.origin_nlc,
                    dest_nlc=flow.dest_nlc,
                    old_pence=fare.fare_pence,
                    discount_category=tty_rec.discount_category,
                    fare_line_no=fare.line_no,
                    flow_line_no=flow.line_no,
                )
            contributed = True
        if contributed:
            station_union.update(entity_to_members.get(flow.origin_nlc) or {flow.origin_nlc})
            station_union.update(entity_to_members.get(flow.dest_nlc) or {flow.dest_nlc})

    if not rows:
        notes.append(
            f"no fares matched operator scope (toc={toc_code}) × "
            f"discount_categories={list(change.discount_categories)}; "
            "ChangeRequest is a no-op against this feed snapshot"
        )

    canonical: list[AffectedFare] = []
    # Per-row fan-out kept aside so blast pairs can be emitted AFTER ranking
    # (below): (o_members, d_members, reason), indexed by canonical position.
    expansions: list[tuple[list[str], list[str], ExpansionReason]] = []
    blast_total = 0
    for key in sorted(rows):
        r = rows[key]
        new_pence, synth_step = apply_synthetic_railcard(r.old_pence, change)
        o_members = sorted(entity_to_members.get(r.origin_nlc) or {r.origin_nlc})
        d_members = sorted(entity_to_members.get(r.dest_nlc) or {r.dest_nlc})
        n_pairs = len(o_members) * len(d_members)
        blast_total += n_pairs
        o_kind = _expansion_kind(r.origin_nlc, entity_to_members, loc)
        d_kind = _expansion_kind(r.dest_nlc, entity_to_members, loc)
        reason = _reason_from_kinds(o_kind, d_kind)
        provenance: tuple[ProvenanceStep, ...] = (
            ProvenanceStep(
                step="affected_set_pick",
                source=f"{feed_paths.ffl.name} flow_id={r.flow_id}",
                detail={
                    "scope":             "toc",
                    "toc":               toc_code,
                    "ticket_code":       r.ticket_code,
                    "route_code":        r.route_code,
                    "representative":    f"{r.origin_nlc}->{r.dest_nlc}",
                    "flow_origin_code":  r.origin_nlc,
                    "flow_dest_code":    r.dest_nlc,
                    "blast_pairs_count": str(n_pairs),
                    "fare_line_no":      str(r.fare_line_no),
                    "flow_line_no":      str(r.flow_line_no),
                    "explanation":       (
                        f"flow selected from FFLIndexes.flows_by_toc[{toc_code!r}] "
                        "(usage_code='A'); .TTY DISCOUNT_CATEGORY in scope"
                    ),
                },
            ),
            synth_step,
        )
        blast_stations = set(o_members) | set(d_members)
        canonical.append(AffectedFare(
            flow_id=r.flow_id,
            ticket_code=r.ticket_code,
            route_code=r.route_code,
            representative_origin_nlc=r.origin_nlc,
            representative_dest_nlc=r.dest_nlc,
            status="resolved",
            old_price_pence=r.old_pence,
            new_price_pence=new_pence,
            discount_category=r.discount_category,
            provenance=provenance,
            blast_radius_pairs=tuple(
                (mo, md) for mo in o_members for md in d_members
            )[:_TOC_ROW_PAIR_CAP],
            representative_origin_name=_loc_name(r.origin_nlc, loc),
            representative_dest_name=_loc_name(r.dest_nlc, loc),
            blast_station_nlcs=tuple(sorted(blast_stations)[:200]),
        ))
        expansions.append((o_members, d_members, reason))

    # Emit blast pairs in the SAME top-|Δ| ranking report.py uses to truncate
    # rows, so the capped pair budget lands on rows that survive the cut
    # (canonical-order emission would spend it all on rows about to be dropped).
    def _rank_delta(f: AffectedFare) -> int:
        if f.new_price_pence is None or f.old_price_pence is None:
            return 0
        return abs(f.new_price_pence - f.old_price_pence)

    ranked = sorted(
        range(len(canonical)),
        key=lambda i: (-_rank_delta(canonical[i]),
                       canonical[i].flow_id, canonical[i].ticket_code),
    )
    blast_out: list[BlastRadiusPair] = []
    blast_capped = False
    for idx in ranked:
        if len(blast_out) >= _TOC_BLAST_PAIR_CAP:
            blast_capped = True
            break
        o_members, d_members, reason = expansions[idx]
        emitted_this_row = 0
        row_done = False
        for mo in o_members:
            if row_done:
                break
            for md in d_members:
                if len(blast_out) >= _TOC_BLAST_PAIR_CAP:
                    blast_capped = True
                    row_done = True
                    break
                if emitted_this_row >= _TOC_ROW_PAIR_CAP:
                    row_done = True  # per-row cut; counted via blast_total
                    break
                blast_out.append(BlastRadiusPair(
                    origin_nlc=mo, dest_nlc=md,
                    canonical_index=idx,
                    expansion_reason=reason,
                ))
                emitted_this_row += 1

    if blast_capped or blast_total > len(blast_out):
        notes.append(
            f"blast-radius pairs capped at operator scope: {len(blast_out)} "
            f"of {blast_total} emitted (cap {_TOC_BLAST_PAIR_CAP} total, "
            f"{_TOC_ROW_PAIR_CAP} per row); station union and aggregates are uncut"
        )

    return AffectedSet(
        canonical=tuple(canonical),
        skipped=tuple(),
        blast_radius=tuple(blast_out),
        notes=tuple(notes),
        stats=ScopeStats(
            scope="toc",
            toc_code=toc_code,
            flows_total=len(all_flows),
            flows_actual=len(actual),
            flows_generated_skipped=skipped_g,
            canonical_total=len(canonical),
            canonical_returned=len(canonical),
            blast_pairs_total=blast_total,
            blast_pairs_returned=len(blast_out),
            truncated=False,  # row truncation happens in report.py, which updates this
            toc_station_nlcs=tuple(sorted(station_union)[:_TOC_STATION_CAP]),
        ),
    )


def _loc_name(nlc: str, loc: dict[str, LocationMeta]) -> str:
    meta = loc.get(nlc)
    return meta.station_name.strip() if meta is not None else nlc


# Priority for merging a pair's per-side expansion kind: a pair re-derived
# through a wider fan-out keeps its most direct explanation.
_KIND_RANK = {"direct": 0, "loc_group": 1, "fsc_cluster": 2}


def _expand_via_loc_and_fsc(
    nlc: str,
    loc: dict[str, LocationMeta],
    fsc: dict[str, list[str]],
) -> tuple[list[str], dict[str, str]]:
    """Mirror src/resolver/resolve.py:_expand: [self, LOC group, *FSC
    clusters], deduped, order-preserving. Also returns each candidate's
    expansion kind ("direct" | "loc_group" | "fsc_cluster") for blast-pair
    reason tagging."""
    out = [nlc]
    kind = {nlc: "direct"}
    meta = loc.get(nlc)
    if meta is not None and meta.group_nlc.strip() and meta.group_nlc != nlc:
        out.append(meta.group_nlc)
        kind[meta.group_nlc] = "loc_group"
    for cluster_id in fsc.get(nlc, []):
        if cluster_id not in kind:
            out.append(cluster_id)
            kind[cluster_id] = "fsc_cluster"
    return out, kind


def _reason_from_kinds(o_kind: str, d_kind: str) -> Literal[
    "direct",
    "loc_group_origin", "loc_group_dest", "loc_group_both",
    "fsc_cluster_origin", "fsc_cluster_dest", "fsc_cluster_both",
]:
    """Collapse per-side expansion kinds into one blast-pair reason. When
    both sides were expanded, an FSC cluster on either side wins the label
    (the rarer, more surprising mechanism is the one worth surfacing)."""
    if o_kind == "direct" and d_kind == "direct":
        return "direct"
    if d_kind == "direct":
        return "fsc_cluster_origin" if o_kind == "fsc_cluster" else "loc_group_origin"
    if o_kind == "direct":
        return "fsc_cluster_dest" if d_kind == "fsc_cluster" else "loc_group_dest"
    if "fsc_cluster" in (o_kind, d_kind):
        return "fsc_cluster_both"
    return "loc_group_both"


__all__ = [
    "AffectedFare", "BlastRadiusPair", "AffectedSet", "ScopeStats",
    "compute_affected_set",
]
