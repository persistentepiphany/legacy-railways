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
from typing import TYPE_CHECKING, Literal

from src.ingest.inspect import (
    FFLIndexes,
    LocationMeta,
    TtyRecord,
    load_ffl_indexes,
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


@dataclass(frozen=True)
class BlastRadiusPair:
    """One (origin, dest) reachable through cluster fan-out, with a back-link
    to the canonical row it inherits."""
    origin_nlc: str
    dest_nlc: str
    canonical_index: int
    expansion_reason: Literal["direct", "loc_group_origin", "loc_group_dest", "loc_group_both"]


@dataclass(frozen=True)
class AffectedSet:
    """The result of compute_affected_set."""
    canonical: tuple[AffectedFare, ...]
    skipped: tuple[AffectedFare, ...]
    blast_radius: tuple[BlastRadiusPair, ...]
    notes: tuple[str, ...]


def compute_affected_set(change: ChangeRequest, feed_paths: FeedPaths) -> AffectedSet:
    """Walk the corridor's cluster cross-product; produce canonical rows +
    blast-radius pairs. Pure-ish: reads the feed via mtime-cached loaders.

    Algorithm:
      1. Expand origin/dest to their LOC-group sets ([self, group]).
      2. Walk `flows_by_pair` for the cross-product, both directions (with
         the DIRECTION='R' filter on the reverse leg).
      3. Filter fares by .TTY DISCOUNT_CATEGORY (the change's scope).
      4. Group hits by (flow_id, ticket_code) → one AffectedFare per group.
      5. For each AffectedFare, derive its blast_radius_pairs by expanding
         the flow's (origin, dest) into member NLCs (LOC reverse lookup).
    """
    ffl = load_ffl_indexes(feed_paths.ffl)
    loc = load_loc_meta(feed_paths.loc)
    tty = load_ticket_type_meta(feed_paths.tty)

    notes: list[str] = []
    scope = set(change.discount_categories)

    # Reverse LOC: GROUP_NLC → set of MEMBER_NLCs (so we know who 0438 contains).
    group_to_members: dict[str, set[str]] = defaultdict(set)
    for nlc, meta in loc.items():
        if meta.group_nlc.strip():
            group_to_members[meta.group_nlc].add(nlc)
    # A leaf NLC (e.g. 2968) is its own one-member "cluster" for blast-radius
    # purposes: a direct-flow fare governs exactly itself.

    origin_expansion = _expand_via_loc(change.corridor_origin_nlc, loc)
    dest_expansion = _expand_via_loc(change.corridor_dest_nlc, loc)

    @dataclass
    class _Accum:
        """Aggregator for one (flow_id, ticket_code) before becoming an AffectedFare."""
        flow_id: str
        ticket_code: str
        route_code: str
        ffl_old_pence: int
        rep_origin: str
        rep_dest: str
        blast_pairs: set[tuple[str, str]]

    accum: dict[tuple[str, str], _Accum] = {}

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
                        blast_pairs=set(),
                    )
                    accum[key] = rec
                # Blast radius: the flow's (flow_origin, flow_dest) governs
                # every (member_o, member_d) station pair via .LOC group
                # membership. For a leaf NLC group_to_members has no entry,
                # so we fall back to {nlc}.
                o_members = group_to_members.get(flow_origin) or {flow_origin}
                d_members = group_to_members.get(flow_dest) or {flow_dest}
                for mo in o_members:
                    for md in d_members:
                        rec.blast_pairs.add((mo, md))

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
                            blast_pairs=set(),
                        )
                        accum[key] = rec
                    # For reverse flows the blast radius uses the flow's
                    # native (origin, dest); we map them back to the customer-
                    # facing demand direction (o, d) by *swapping*.
                    o_members = group_to_members.get(d) or {d}
                    d_members = group_to_members.get(o) or {o}
                    for mo in o_members:
                        for md in d_members:
                            rec.blast_pairs.add((mo, md))

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
                    "blast_pairs_count": str(len(rec.blast_pairs)),
                    "explanation":       (
                        "flow_id selected from FFLIndexes.flows_by_pair after "
                        f"LOC group fan-out; .TTY DISCOUNT_CATEGORY in scope"
                    ),
                },
            ),
            synth_step,
        )
        idx = len(canonical)
        tty_rec = tty.get(rec.ticket_code)
        assert tty_rec is not None  # filtered above
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
        ))
        for (mo, md) in sorted(rec.blast_pairs):
            reason: Literal["direct", "loc_group_origin", "loc_group_dest", "loc_group_both"]
            if (mo, md) == (change.corridor_origin_nlc, change.corridor_dest_nlc):
                reason = "direct"
            elif mo == change.corridor_origin_nlc:
                reason = "loc_group_dest"
            elif md == change.corridor_dest_nlc:
                reason = "loc_group_origin"
            else:
                reason = "loc_group_both"
            blast_pairs_out.append(BlastRadiusPair(
                origin_nlc=mo, dest_nlc=md,
                canonical_index=idx, expansion_reason=reason,
            ))

    # Stable, deterministic ordering by (origin, dest, canonical_index).
    blast_pairs_out.sort(key=lambda p: (p.origin_nlc, p.dest_nlc, p.canonical_index))

    return AffectedSet(
        canonical=tuple(canonical),
        skipped=tuple(),  # bulk path has no skips (every fare has an int price)
        blast_radius=tuple(blast_pairs_out),
        notes=tuple(notes),
    )


def _expand_via_loc(nlc: str, loc: dict[str, LocationMeta]) -> list[str]:
    """Mirror src/resolver/resolve.py:_expand for LOC group fan-out (only)."""
    out = [nlc]
    meta = loc.get(nlc)
    if meta is not None and meta.group_nlc.strip() and meta.group_nlc != nlc:
        out.append(meta.group_nlc)
    return out


__all__ = ["AffectedFare", "BlastRadiusPair", "AffectedSet", "compute_affected_set"]
