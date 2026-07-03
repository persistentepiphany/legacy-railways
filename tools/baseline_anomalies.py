"""Baseline structural anomalies — run the inversion detectors against the
current feed snapshot with NO change applied.

Surfaces "what's broken in the fares graph right now": returns priced below
singles, first-class priced at/below standard, on a corridor (or sweep of
corridors). This is the same detector suite the change-path uses
(`src/impact/inversions.py`), fed a synthetic affected-set where
new_price == old_price so the comparisons run on baseline prices directly.

Run from the repo root:

    python tools/baseline_anomalies.py
    python tools/baseline_anomalies.py 2968 1444
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.impact.affected import AffectedFare
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import detect_inversions
from src.ingest.inspect import (
    load_ffl_indexes,
    load_loc_meta,
    load_ticket_type_meta,
)
from src.resolver.resolve import ProvenanceStep


HEADLINE_CORRIDORS: list[tuple[str, str, str]] = [
    ("MAN -> EUS",  "2968", "1444"),
    ("EDB -> KGX",  "9328", "6121"),
    ("LDS -> KGX",  "8487", "6121"),
    ("BRI -> PAD",  "3231", "3087"),
    ("CDF -> PAD",  "3899", "3087"),
    ("GLC -> EDB",  "9813", "9328"),
    ("YRK -> NCL",  "8263", "7728"),
    ("LIV -> EUS",  "2246", "1444"),
    ("BHM -> EUS",  "1127", "1444"),
    ("NCL -> EDB",  "7728", "9328"),
]


def baseline_affected(
    origin_nlc: str,
    dest_nlc: str,
    feed_paths: FeedPaths,
) -> tuple[AffectedFare, ...]:
    """Walk every flow on the (origin, dest) cluster cross-product and emit one
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

    def emit(rep_o: str, rep_d: str, flow_o: str, flow_d: str) -> None:
        for flow in ffl.flows_by_pair.get((flow_o, flow_d), []):
            for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                key = (flow.flow_id, fare.ticket_code)
                if key in rows:
                    continue
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
            emit(o, d, o, d)
            for flow in ffl.flows_by_pair.get((d, o), []):
                if flow.direction != "R":
                    continue
                for fare in ffl.fares_by_flow.get(flow.flow_id, []):
                    key = (flow.flow_id, fare.ticket_code)
                    if key in rows:
                        continue
                    rows[key] = AffectedFare(
                        flow_id=flow.flow_id,
                        ticket_code=fare.ticket_code,
                        route_code=flow.route_code,
                        representative_origin_nlc=o,
                        representative_dest_nlc=d,
                        status="resolved",
                        old_price_pence=fare.fare_pence,
                        new_price_pence=fare.fare_pence,
                        discount_category="",
                        provenance=prov,
                        blast_radius_pairs=(),
                    )

    return tuple(sorted(rows.values(), key=lambda f: (f.flow_id, f.ticket_code)))


def scan_one(label: str, origin_nlc: str, dest_nlc: str, feed_paths: FeedPaths) -> None:
    affected = baseline_affected(origin_nlc, dest_nlc, feed_paths)
    inversions = detect_inversions(affected, feed_paths)
    print(f"\n=== {label}  ({origin_nlc} -> {dest_nlc})  "
          f"fares_scanned={len(affected)}  inversions={len(inversions)} ===")
    if not inversions:
        print("  (no structural inversions on baseline)")
        return
    tty = load_ticket_type_meta(feed_paths.tty)
    by_rule: dict[str, list] = {}
    for inv in inversions:
        by_rule.setdefault(inv.rule, []).append(inv)
    for rule, hits in by_rule.items():
        print(f"  [{rule}] {len(hits)}")
        for inv in hits[:3]:
            h_meta = tty.get(inv.higher_ticket.replace("-child", ""))
            l_meta = tty.get(inv.lower_ticket.replace("-child", ""))
            h_desc = h_meta.description.strip() if h_meta else ""
            l_desc = l_meta.description.strip() if l_meta else ""
            print(f"    {inv.higher_ticket} (£{inv.higher_price_pence/100:.2f}, {h_desc})"
                  f" > {inv.lower_ticket} (£{inv.lower_price_pence/100:.2f}, {l_desc})")
        if len(hits) > 3:
            print(f"    ... +{len(hits) - 3} more")


def main(argv: list[str]) -> int:
    paths = FeedPaths.default_for_data_dir(REPO_ROOT / "data")
    missing = paths.missing()
    if missing:
        print(f"missing feed file(s): {missing}", file=sys.stderr)
        return 1

    if len(argv) == 3:
        scan_one(f"{argv[1]} -> {argv[2]}", argv[1], argv[2], paths)
        return 0
    if len(argv) != 1:
        print("usage: python tools/baseline_anomalies.py [origin_nlc dest_nlc]",
              file=sys.stderr)
        return 2

    for label, o, d in HEADLINE_CORRIDORS:
        scan_one(label, o, d, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
