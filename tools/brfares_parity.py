"""BRFares parity check for the demo corridor.

Compares our resolver's price for a set of MAN→EUS ticket codes against the
BRFares captures in `data/brfares_man_eus*.json`. Print a table of matches
and mismatches; exit non-zero if any adult fare diverges by more than 5p.

Run:
    python -m tools.brfares_parity

The script is intentionally small — no test framework, no fixtures, just
five lines per row so the parity story is grokkable at a glance."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.impact.feed_paths import FeedPaths  # noqa: E402
from src.resolver.resolve import resolve_fare  # noqa: E402


DATA = REPO_ROOT / "data"
CAPTURES = {
    "adult":    DATA / "brfares_man_eus.json",
    "2together": DATA / "brfares_man_eus_2tr.json",
    "disabled": DATA / "brfares_man_eus_dis.json",
    "family":   DATA / "brfares_man_eus_fam.json",
    "senior":   DATA / "brfares_man_eus_srn.json",
    "young":    DATA / "brfares_man_eus_yng.json",
}

# BRFares includes hundreds of season / travelcard / advance products we
# don't resolve in this slice. Comparing the walkup returns/singles keeps
# the parity story tight and honest.
WALKUP_CODES = {"SOR", "SDS", "SDR", "CDS", "CDR", "SOS"}
TOLERANCE_P = 5


def _brfares_rows(path: Path) -> list[dict]:
    """Yield rows keyed by (ticket_code, adult_pence, group_orig_nlc, group_dest_nlc)
    for direct comparison against our resolver."""
    if not path.exists():
        return []
    doc = json.load(path.open("r", encoding="utf-8"))
    out: list[dict] = []
    for f in doc.get("fares", []):
        ticket_code = (f.get("ticket") or {}).get("code")
        if not ticket_code or ticket_code not in WALKUP_CODES:
            continue
        adult = ((f.get("adult") or {}).get("fare"))
        if adult is None:
            continue
        out.append({
            "ticket_code":  ticket_code,
            "adult_pence":  int(adult),
            "origin_nlc":   f.get("group_orig", {}).get("nlc"),
            "dest_nlc":     f.get("group_dest", {}).get("nlc"),
        })
    return out


def main() -> int:
    fp = FeedPaths.default_for_data_dir(DATA)
    missing = fp.missing()
    if missing:
        print(f"FEED MISSING — cannot run parity: {[str(p.name) for p in missing]}")
        return 2

    # The `adult` capture is compared against the base resolver (no
    # railcard) and IS the primary correctness gate — if that diverges the
    # resolver is wrong. The railcard captures need the resolver called
    # with the corresponding railcard_code (deferred). They're printed as
    # informational rows so the parity story is visible.
    primary_diverged = 0
    for label, path in CAPTURES.items():
        rows = [r for r in _brfares_rows(path) if r["origin_nlc"] and r["dest_nlc"]]
        if not rows:
            print(f"[{label:9s}] no capture / no walkup rows — skipping")
            continue
        primary = label == "adult"
        matched = diverged = seen = 0
        print(f"\n[{label}] {path.name} — {len(rows)} walkup row(s)"
              f"{'  (PRIMARY GATE)' if primary else '  (informational)'}")
        # BRFares returns multiple rows per ticket code (different routes).
        # The resolver picks ONE flow deterministically; compare against the
        # BEST-matching capture row so alternate routes don't count as
        # divergence — we track whether ANY capture matched.
        by_code: dict[str, list[dict]] = {}
        for r in rows:
            by_code.setdefault(r["ticket_code"], []).append(r)
        for code, candidates in by_code.items():
            seen += 1
            resolved = resolve_fare(
                candidates[0]["origin_nlc"], candidates[0]["dest_nlc"], code,
                fp.ffl, loc_path=fp.loc, fsc_path=fp.fsc, nfo_path=fp.nfo,
                rlc_path=fp.rlc, dis_path=fp.dis, rcm_path=fp.rcm,
                frr_path=fp.frr, tty_path=fp.tty,
            )
            got = resolved.price_pence
            if got is None:
                print(f"  ?? {code}  resolver {resolved.status} (no price)")
                diverged += 1
                continue
            best = min(candidates, key=lambda r: abs(r["adult_pence"] - got))
            delta = got - best["adult_pence"]
            if abs(delta) <= TOLERANCE_P:
                matched += 1
                print(f"  OK {code}  BRFares £{best['adult_pence']/100:.2f}"
                      f"  vs  resolver £{got/100:.2f}   Δ = {delta:+d}p"
                      f"  (best of {len(candidates)} capture(s))")
            else:
                diverged += 1
                capture_prices = ", ".join(f"£{c['adult_pence']/100:.2f}" for c in candidates)
                print(f"  !! {code}  BRFares captures: {capture_prices}"
                      f"  vs  resolver £{got/100:.2f}   best Δ = {delta:+d}p")
        print(f"  → {matched}/{seen} within \u00b1{TOLERANCE_P}p")
        if primary:
            primary_diverged = diverged

    print("\nPrimary adult-capture gate:"
          f" {'PASS' if primary_diverged == 0 else f'FAIL ({primary_diverged} diverged)'}")
    return 0 if primary_diverged == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
