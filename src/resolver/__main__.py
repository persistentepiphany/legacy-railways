"""Demo entry point: resolve one fare with provenance.
Run with `python -m src.resolver`.

Defaults: MANCHESTER PICCADILLY (2968) -> LONDON EUSTON (1444), ticket SOR
(Standard Off-Peak Return). With the .LOC file the resolver expands these
station-level NLCs to their groups (0438 / 1072) for the blast-radius fan-out.

Optional --route picks a specific route (00000=ANY PERMITTED, 00129=VIA
CHESTERFIELD, etc.); without it the resolver prefers ANY PERMITTED.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.resolver.resolve import ResolvedFare, resolve_fare


def _format(result: ResolvedFare) -> str:
    """Pretty-print a ResolvedFare for the demo."""
    lines: list[str] = []
    header = (
        f"{result.origin_nlc} -> {result.dest_nlc}  ticket={result.ticket_code}  "
        f"status={result.status}"
    )
    lines.append(header)
    lines.append("=" * len(header))
    if result.price_pence is not None:
        lines.append(
            f"PRICE: {result.price_pence} pence  (£{result.price_pence / 100:.2f})"
        )
    else:
        lines.append("PRICE: (unresolved — see provenance for why)")
    lines.append("")
    lines.append("PROVENANCE CHAIN")
    lines.append("-" * 16)
    for i, step in enumerate(result.provenance, start=1):
        lines.append(f"[{i}] {step.step}  @  {step.source}")
        for k, v in step.detail.items():
            lines.append(f"      {k} = {v}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.resolver",
        description="Resolve one fare with full provenance (thin slice).",
    )
    parser.add_argument("--origin", default="2968", help="Origin NLC (default: 2968 = MANCHESTER PICCADILLY)")
    parser.add_argument("--dest",   default="1444", help="Dest NLC (default: 1444 = LONDON EUSTON)")
    parser.add_argument("--ticket", default="SOR",  help="Ticket code (default: SOR = Standard Off-Peak Return)")
    parser.add_argument(
        "--feed",
        type=Path,
        default=Path("data/RJFAF805.FFL"),
        help="Path to a .FFL file (default: data/RJFAF805.FFL).",
    )
    parser.add_argument(
        "--loc",
        type=Path,
        default=Path("data/RJFAF805.LOC"),
        help="Path to a .LOC file for group fan-out (default: data/RJFAF805.LOC). Pass empty to disable.",
    )
    parser.add_argument(
        "--fsc",
        type=Path,
        default=Path("data/RJFAF805.FSC"),
        help="Path to a .FSC file for cluster fan-out (default: data/RJFAF805.FSC). Pass empty to disable.",
    )
    parser.add_argument(
        "--nfo",
        type=Path,
        default=Path("data/RJFAF805.NFO"),
        help="Path to a .NFO file for override layer (default: data/RJFAF805.NFO). Pass empty to disable.",
    )
    parser.add_argument("--rlc", type=Path, default=Path("data/RJFAF805.RLC"),
                        help="Path to a .RLC file (railcards). Required when --railcard is given.")
    parser.add_argument("--dis", type=Path, default=Path("data/RJFAF805.DIS"),
                        help="Path to a .DIS file (status discounts). Required when --railcard is given.")
    parser.add_argument("--rcm", type=Path, default=Path("data/RJFAF805.RCM"),
                        help="Path to a .RCM file (railcard min fares). Required when --railcard is given.")
    parser.add_argument("--frr", type=Path, default=Path("data/RJFAF805.FRR"),
                        help="Path to a .FRR file (rounding rules). Required when --railcard is given.")
    parser.add_argument("--tty", type=Path, default=Path("data/RJFAF805.TTY"),
                        help="Path to a .TTY file (ticket types). Required when --railcard is given.")
    parser.add_argument(
        "--route",
        default=None,
        help="Route code (5 chars, e.g. 00000=ANY PERMITTED, 00129=VIA CHESTERFIELD). Default: prefer 00000.",
    )
    parser.add_argument(
        "--railcard",
        default=None,
        help="Railcard code (3 chars, e.g. YNG=16-25 Young Persons). Default: no railcard (adult).",
    )
    args = parser.parse_args(argv)

    if not args.feed.exists():
        print(f"error: feed file not found: {args.feed}")
        return 2

    def _opt(p: Path | None) -> Path | None:
        return p if (p and str(p) and p.exists()) else None
    loc_path = _opt(args.loc)
    fsc_path = _opt(args.fsc)
    nfo_path = _opt(args.nfo)
    rlc_path = _opt(args.rlc)
    dis_path = _opt(args.dis)
    rcm_path = _opt(args.rcm)
    frr_path = _opt(args.frr)
    tty_path = _opt(args.tty)
    result = resolve_fare(
        args.origin, args.dest, args.ticket, args.feed,
        loc_path=loc_path, fsc_path=fsc_path, nfo_path=nfo_path,
        rlc_path=rlc_path, dis_path=dis_path, rcm_path=rcm_path,
        frr_path=frr_path, tty_path=tty_path,
        route_code=args.route, railcard_code=args.railcard,
    )
    print(_format(result))
    return 0 if result.status == "resolved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
