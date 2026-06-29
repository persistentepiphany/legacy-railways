"""Compare resolver output to BRFares oracle JSONs end-to-end.

Globs `data/brfares_man_eus_*.json`, infers the railcard code from the
filename suffix (e.g. `brfares_man_eus_yng.json` -> "YNG"), and for every
fare row calls `resolve_fare` with the matching ticket/route/railcard.
Reports a summary table per file: match / mismatch / quarantined.

Reads the corridor (orig/dest NLCs) from each JSON's `group_orig.nlc` /
`group_dest.nlc` so it works for any MAN-EUS variant without code edits.

Skip with `--rlc YNG SRN` to limit which files are compared.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.resolver.resolve import ResolvedFare, resolve_fare  # noqa: E402

DATA = REPO_ROOT / "data"
FEED = DATA / "RJFAF805.FFL"
LOC = DATA / "RJFAF805.LOC"
FSC = DATA / "RJFAF805.FSC"
NFO = DATA / "RJFAF805.NFO"
RLC = DATA / "RJFAF805.RLC"
DIS = DATA / "RJFAF805.DIS"
RCM = DATA / "RJFAF805.RCM"
FRR = DATA / "RJFAF805.FRR"
TTY = DATA / "RJFAF805.TTY"

# NLC of a specific station inside the orig/dest group — exercises full
# LOC GROUP_NLC + FSC cluster fan-out, matching how the existing oracle
# test queries.
ORIG_STATION_NLC = "2968"  # MANCHESTER PICCADILLY
DEST_STATION_NLC = "1444"  # LONDON EUSTON


def _resolve_one(ticket: str, route: str, railcard: str | None) -> ResolvedFare:
    return resolve_fare(
        ORIG_STATION_NLC, DEST_STATION_NLC, ticket, FEED,
        loc_path=LOC, fsc_path=FSC, nfo_path=NFO,
        rlc_path=RLC, dis_path=DIS, rcm_path=RCM, frr_path=FRR, tty_path=TTY,
        route_code=route, railcard_code=railcard,
    )


def _compare_one(path: Path, limit: int | None) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    rlc_field = payload.get("railcard") or {}
    railcard = rlc_field.get("code", "").strip() or None
    rows = payload.get("fares") or []
    if limit is not None:
        rows = rows[:limit]

    summary: dict[str, Any] = {
        "file": path.name,
        "railcard": railcard or "(adult)",
        "rows": len(rows),
        "match": 0,
        "mismatch": [],            # (ticket, route_str, expected, got, delta)
        "quarantined": 0,          # resolver returned status != "resolved"
        "delta_buckets": Counter(),
    }
    for row in rows:
        ticket = (row.get("ticket") or {}).get("code")
        if not ticket:
            continue
        route_int = (row.get("route") or {}).get("code", 0)
        route_str = f"{int(route_int):05d}"
        # `raw_price` is the adult base; `adult.fare` is the railcard-discounted
        # price for the queried railcard. When the railcard doesn't apply to a
        # given ticket, BRFares still populates adult.fare with the adult
        # price (and adult.status.code reverts to 000). Validating the chain
        # means matching adult.fare specifically.
        adult = row.get("adult") or {}
        expected = adult.get("fare") if railcard else row.get("raw_price")
        if expected is None:
            continue
        try:
            result = _resolve_one(ticket, route_str, railcard)
        except Exception as exc:
            summary["mismatch"].append((ticket, route_str, expected, None, f"ERROR: {exc!r}"))
            continue
        if result.status != "resolved":
            summary["quarantined"] += 1
            continue
        got = result.price_pence
        if got == expected:
            summary["match"] += 1
        else:
            delta = (got or 0) - expected
            summary["mismatch"].append((ticket, route_str, expected, got, delta))
            summary["delta_buckets"][delta] += 1
    return summary


def _print_summary(s: dict[str, Any], *, verbose_mismatches: int) -> None:
    print(f"\n== {s['file']}  rlc={s['railcard']}  rows={s['rows']} ==")
    n_mm = len(s["mismatch"])
    print(f"  match={s['match']}  mismatch={n_mm}  quarantined={s['quarantined']}")
    if s["delta_buckets"]:
        # Most-common deltas first; tells us whether mismatches cluster around
        # a single rounding/discount error or are scattered.
        top = ", ".join(
            f"{d:+d}p×{n}" for d, n in s["delta_buckets"].most_common(8)
        )
        print(f"  delta buckets: {top}")
    if n_mm and verbose_mismatches:
        for ticket, route, expected, got, delta in s["mismatch"][:verbose_mismatches]:
            print(f"    {ticket} route={route}  exp={expected}p  got={got}p  Δ={delta}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rlc", nargs="*", help="Limit to these railcard codes (e.g. YNG SRN)")
    p.add_argument("--limit", type=int, default=None, help="Limit rows per file (smoke)")
    p.add_argument("--show-mismatches", type=int, default=10, help="Per-file mismatch lines to print")
    args = p.parse_args()

    files = sorted(DATA.glob("brfares_man_eus_*.json"))
    if args.rlc:
        wanted = {r.lower() for r in args.rlc}
        files = [f for f in files if f.stem.split("_")[-1].lower() in wanted]
    if not files:
        print("no matching brfares_man_eus_*.json files in data/", file=sys.stderr)
        return 2

    grand_total = grand_match = grand_mm = grand_q = 0
    for f in files:
        s = _compare_one(f, args.limit)
        _print_summary(s, verbose_mismatches=args.show_mismatches)
        grand_total += s["rows"]
        grand_match += s["match"]
        grand_mm += len(s["mismatch"])
        grand_q += s["quarantined"]

    print(f"\n== TOTAL across {len(files)} files ==")
    print(f"  rows={grand_total}  match={grand_match}  mismatch={grand_mm}  quarantined={grand_q}")
    pct = 100 * grand_match / grand_total if grand_total else 0
    print(f"  match rate on resolved fares: {pct:.1f}%")
    return 0 if grand_mm == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
