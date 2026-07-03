"""Baseline structural-anomaly scan — the change-path inversion detectors
(`src/impact/inversions.py`) run against the CURRENT feed snapshot with no
change applied. Emits a synthetic affected-set where new_price == old_price
so the R1/R2/R3 comparisons operate on baseline prices directly.

Pure and deterministic; reads the feed via the cached ingest indexes.
"""

from __future__ import annotations

from src.impact.affected import AffectedFare
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.ingest.inspect import load_ffl_indexes, load_loc_meta
from src.resolver.resolve import ProvenanceStep

__all__ = ["baseline_affected", "scan_baseline"]


def baseline_affected(
    origin_nlc: str,
    dest_nlc: str,
    feed_paths: FeedPaths,
) -> tuple[AffectedFare, ...]:
    """Walk every flow on the (origin, dest) group cross-product and emit one
    AffectedFare per (flow_id, ticket_code) with new_price == old_price.

    Mirrors src/impact/affected.py:compute_affected_set's flow walk but skips
    the .TTY scope filter and the synthetic-discount step — we want the
    baseline prices, not a repriced set."""
    ffl = load_ffl_indexes(feed_paths.ffl)
    loc = load_loc_meta(feed_paths.loc)

    o_expand = [origin_nlc]
    if (m := loc.get(origin_nlc)) and m.group_nlc.strip() and m.group_nlc != origin_nlc:
        o_expand.append(m.group_nlc)
    d_expand = [dest_nlc]
    if (m := loc.get(dest_nlc)) and m.group_nlc.strip() and m.group_nlc != dest_nlc:
        d_expand.append(m.group_nlc)

    rows: dict[tuple[str, str], AffectedFare] = {}
    prov = (ProvenanceStep(
        step="baseline_scan",
        source=f"{feed_paths.ffl.name}",
        detail={"explanation": "baseline anomaly scan; new_price == old_price"},
    ),)

    def add(rep_o: str, rep_d: str, flow, fare) -> None:
        key = (flow.flow_id, fare.ticket_code)
        if key in rows:
            return
        rows[key] = AffectedFare(
            flow_id=flow.flow_id,
            ticket_code=fare.ticket_code,
            route_code=flow.route_code,
            representative_origin_nlc=rep_o,
            representative_dest_nlc=rep_d,
            status="resolved",
            old_price_pence=fare.fare_pence,
            new_price_pence=fare.fare_pence,
            discount_category="",
            provenance=prov,
            blast_radius_pairs=(),
        )

    for o in o_expand:
        for d in d_expand:
            for flow in ffl.flows_by_pair.get((o, d), []):
                for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                    add(o, d, flow, fare)
            # §3.2 DIRECTION='R': a reversible flow stored dest->origin
            # also prices the origin->dest journey.
            for flow in ffl.flows_by_pair.get((d, o), []):
                if flow.direction != "R":
                    continue
                for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                    add(o, d, flow, fare)

    return tuple(sorted(rows.values(), key=lambda f: (f.flow_id, f.ticket_code)))


def scan_baseline(
    origin_nlc: str,
    dest_nlc: str,
    feed_paths: FeedPaths,
) -> tuple[tuple[FareInversion, ...], int]:
    """Convenience: (inversions, fares_scanned) for one corridor."""
    affected = baseline_affected(origin_nlc, dest_nlc, feed_paths)
    return detect_inversions(affected, feed_paths), len(affected)
