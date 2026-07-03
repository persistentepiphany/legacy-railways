"""National split-ticket leaderboard for a single ChangeRequest.

Holds a ChangeRequest fixed (railcard + discount + scope) and sweeps it across
N corridors, calling `src.impact.splits.splits_for_change` for each. Ranks
corridors by the saving the change unlocks via splittable intermediates —
i.e. how much the proposal moves the *splittability of the network*, not just
the headline through-fare.

Run from the repo root:

    python tools/split_leaderboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.impact import ChangeRequest, FeedPaths, splits_for_change
from src.ingest.inspect import (
    load_ffl_indexes,
    load_ticket_type_meta,
)


SCOPE_CATEGORY = "01"
# Standard-class walk-up singles to try, in order of preference. Caller of
# `splits_for_change` picks ONE per corridor; we want the most representative
# walk-up that actually exists on the corridor AND falls in the change's scope.
TICKET_PREFERENCES = ("SOS", "SVS", "SDS", "XAS")


SWEEP_CORRIDORS: list[tuple[str, str, str, tuple[str, ...] | None]] = [
    # (label, origin_nlc, dest_nlc, corridor-specific candidate intermediate NLCs)
    # None means "use the splits.py default" (timetable or WCML whitelist).
    ("MAN -> EUS",  "2968", "1444", None),
    ("BRI -> PAD",  "3231", "3087", ("3271", "3333", "3149")),               # Bath, Swindon, Reading
    ("CDF -> PAD",  "3899", "3087", ("3674", "3230", "3333", "3149")),        # Newport, Bristol Pkwy, Swindon, Reading
    ("GLC -> EDB",  "9813", "9328", ("9691", "9931", "9419")),                # Motherwell, Falkirk High, Haymarket
    ("YRK -> NCL",  "8263", "7728", ("7996", "7877", "7745")),                # Northallerton, Darlington, Durham
    ("LIV -> EUS",  "2246", "1444", ("2291", "1243", "1268", "1378", "1087")),# Runcorn, Crewe, Stafford, MKC, Rugby
    ("BHM -> EUS",  "1127", "1444", ("1030", "1087", "1378")),                # Coventry, Rugby, MKC
    ("NCL -> EDB",  "7728", "9328", ("7791", "9397")),                        # Berwick, Dunbar
    ("LDS -> KGX",  "8487", "6121", ("8591", "6417", "6133", "6092")),        # Wakefield, Doncaster, Peterborough, Stevenage
    ("EDB -> KGX",  "9328", "6121", ("7728", "8263", "6417", "6133", "6092")),# Newcastle, York, Doncaster, Peterborough, Stevenage
]


def _pick_ticket(
    o: str, d: str, scope_cat: str, ffl, tty,
) -> str | None:
    """Pick the first ticket in TICKET_PREFERENCES that (a) exists on the
    corridor's flows and (b) is in scope of the change's discount category."""
    available: set[str] = set()
    for flow in ffl.flows_by_pair.get((o, d), []) + ffl.flows_by_pair.get((d, o), []):
        for fare in ffl.fares_by_flow.get(flow.flow_id, []):
            available.add(fare.ticket_code)
    for tk in TICKET_PREFERENCES:
        meta = tty.get(tk)
        if meta and tk in available and meta.discount_category == scope_cat:
            return tk
    return None


def _max_pre_saving(result) -> int:
    return max(
        (c.saving_pence for c in result.pre_change if c.status == "opportunity"),
        default=0,
    )


def _max_post_saving(result) -> int:
    return max(
        (c.saving_pence for c in result.post_change if c.status == "opportunity"),
        default=0,
    )


def main() -> int:
    paths = FeedPaths.default_for_data_dir(REPO_ROOT / "data")
    missing = paths.missing()
    if missing:
        print(f"missing feed file(s): {missing}", file=sys.stderr)
        return 1

    ffl = load_ffl_indexes(paths.ffl)
    tty = load_ticket_type_meta(paths.tty)

    rows = []
    skipped: list[tuple[str, str]] = []
    for label, o, d, intermediates in SWEEP_CORRIDORS:
        ticket = _pick_ticket(o, d, SCOPE_CATEGORY, ffl, tty)
        if ticket is None:
            skipped.append((label, f"no in-scope ('{SCOPE_CATEGORY}') walk-up single on this corridor"))
            continue
        change = ChangeRequest(
            kind="add_railcard",
            railcard_code="STU",
            discount_pct=1.0 / 3.0,
            discount_categories=(SCOPE_CATEGORY,),
            corridor_origin_nlc=o,
            corridor_dest_nlc=d,
            peak_valid=True,
            description=f"Student card 1/3 off, {label}",
        )
        try:
            result = splits_for_change(change, paths, ticket_code=ticket, intermediates=intermediates)
        except Exception as e:
            print(f"{label:14s}  ERROR: {e}", file=sys.stderr)
            continue
        pre_opps = sum(1 for c in result.pre_change if c.status == "opportunity")
        post_opps = sum(1 for c in result.post_change if c.status == "opportunity")
        unres = sum(1 for c in result.post_change if c.status == "unresolvable")
        rows.append({
            "label":       label,
            "ticket":      result.ticket_code,
            "candidates":  len(result.pre_change),
            "unres":       unres,
            "pre_opps":    pre_opps,
            "post_opps":   post_opps,
            "created":     len(result.created),
            "closed":      len(result.closed),
            "max_pre":     _max_pre_saving(result),
            "max_post":    _max_post_saving(result),
            "result":      result,
        })

    rows.sort(key=lambda r: (-r["max_post"], -r["created"]))

    if skipped:
        print()
        print(f"Skipped {len(skipped)} corridor(s) — change scope empty on this snapshot:")
        for lbl, why in skipped:
            print(f"  {lbl:14s} {why}")

    print()
    print(f"Sweep: Student railcard (STU), 1/3 off, scope=DISCOUNT_CATEGORY '01'")
    print(f"Ranked by max post-change split saving across the {len(rows)} corridors.")
    print()
    header = (
        "corridor       ticket  cands  unres  pre_opp  post_opp  created  closed  "
        "max_pre_£     max_post_£"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['label']:14s} {r['ticket']:6s}  "
            f"{r['candidates']:4d}   {r['unres']:4d}   "
            f"{r['pre_opps']:4d}     {r['post_opps']:4d}      "
            f"{r['created']:4d}    {r['closed']:4d}    "
            f"£{r['max_pre']/100:>8.2f}    £{r['max_post']/100:>8.2f}"
        )

    print()
    if rows:
        top = rows[0]
        result = top["result"]
        print(f"=== Headline: {top['label']} (ticket {top['ticket']}) ===")
        opp = [c for c in result.post_change if c.status == "opportunity"]
        opp.sort(key=lambda c: -c.saving_pence)
        for c in opp[:3]:
            print(
                f"  via {c.intermediate_nlc}: through=£{c.through_price_pence/100:.2f}  "
                f"legs=£{c.leg1_price_pence/100:.2f}+£{c.leg2_price_pence/100:.2f}"
                f"  saving=£{c.saving_pence/100:.2f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
