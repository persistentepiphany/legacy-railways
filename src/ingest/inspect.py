"""Feed inspector: slice fixed-width RDG DTD records into labelled fields.

This is the "see-the-mess" tool. It opens an RDG DTD fares feed file
(`.FFL`, `.FSC`, `.NFO`, `.TTY`), walks each fixed-position record, prints
the named fields, and quarantines malformed records into a rejects list
without crashing. dtd2mysql crashes on bad records; we don't.

Usage:
    python -m src.ingest.inspect --feed path/to/RJFAF.FFL [--filter STR]
                                  [--limit N] [--show-rejects]

The `--filter` is a plain substring match against the parsed field values
(e.g. an NLC like "MAN", a FLOW_ID, or a ticket code like "SOR").

Offsets cite RSPS5045 sections where the section is named in CLAUDE.md.
Where CLAUDE.md is silent on the exact byte range, the offset is marked
`TODO(RSPS5045 §X.Y)` so it can be nailed down once docs/RSPS5045.pdf
is checked in.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


@dataclass
class Reject:
    line_no: int
    reason: str
    raw: str


@dataclass
class InspectResult:
    parsed: list[dict[str, str]] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)


def _slice(line: str, start: int, length: int) -> str:
    """1-indexed slice (RSPS5045 documents positions as 1-indexed)."""
    return line[start - 1 : start - 1 + length]


# --- .FFL F record (flow header) -------------------------------------------
# CLAUDE.md names: origin/dest NLC, route, status, FLOW_ID at pos 43-49,
# USAGE_CODE 'A'/'G', DIRECTION 'R'=reversible. Length of the F record per
# RSPS5045 is 49 chars + RECORD_TYPE prefix; in practice lines are ~50 wide.
_FFL_RECORD_LEN = 50  # TODO(RSPS5045 §4.4): confirm exact line width.


def parse_ffl_f(line: str) -> dict[str, str]:
    """Parse an `F` (flow header) record from a .FFL file. RSPS5045 §4.4."""
    return {
        "RECORD_TYPE":     _slice(line, 1, 1),       # 'F'
        "ORIGIN_CODE":     _slice(line, 3, 4),       # NLC. TODO(§4.4): confirm offset
        "DESTINATION_CODE":_slice(line, 7, 4),       # NLC. TODO(§4.4): confirm offset
        "ROUTE_CODE":      _slice(line, 11, 5),      # TODO(§4.4): confirm offset
        "STATUS_CODE":     _slice(line, 16, 3),      # TODO(§4.4): confirm offset
        "USAGE_CODE":      _slice(line, 19, 1),      # 'A'=actual, 'G'=generated
        "DIRECTION":       _slice(line, 20, 1),      # 'R' = reversible
        "END_DATE":        _slice(line, 21, 8),      # 31122999 = no end
        "START_DATE":      _slice(line, 29, 8),
        "TOC":             _slice(line, 37, 3),      # TODO(§4.4): confirm offset
        "CROSS_LONDON_IND":_slice(line, 40, 1),      # TODO(§4.4): confirm offset
        "NS_DISC_IND":     _slice(line, 41, 1),      # TODO(§4.4): confirm offset
        "PUBLICATION_IND": _slice(line, 42, 1),      # 'Y' = published; used by regulation map
        "FLOW_ID":         _slice(line, 43, 7),      # CLAUDE.md: pos 43-49
    }


def parse_ffl_t(line: str) -> dict[str, str]:
    """Parse a `T` (fare) record from a .FFL file. RSPS5045 §4.4.
    Linked to its F-record parent by FLOW_ID."""
    return {
        "RECORD_TYPE":      _slice(line, 1, 1),      # 'T'
        "FLOW_ID":          _slice(line, 3, 7),      # joins back to F
        "TICKET_CODE":      _slice(line, 10, 3),     # TODO(§4.4): confirm offset
        "FARE":             _slice(line, 13, 8),     # pence
        "RESTRICTION_CODE": _slice(line, 21, 2),     # TODO(§4.4): confirm offset
    }


# --- .FSC station cluster --------------------------------------------------

_FSC_RECORD_LEN = 16  # TODO(RSPS5045 §4.18): confirm exact width.


def parse_fsc(line: str) -> dict[str, str]:
    """Parse a station-cluster row. RSPS5045 §4.18.
    One cluster_id governs many member NLCs — the source of blast-radius fan-out."""
    return {
        "CLUSTER_ID":      _slice(line, 1, 4),
        "CLUSTER_NLC":     _slice(line, 5, 4),
        "END_DATE":        _slice(line, 9, 8),       # TODO(§4.18): confirm
        # Real layout has START_DATE / CLUSTER_NAME too; add when PDF lands.
    }


# --- .NFO non-derivable overrides ------------------------------------------
# CLAUDE.md: COMPOSITE_INDICATOR 'Y'=use this record / 'N'=ignore;
# ADULT_FARE/CHILD_FARE = 99999999 means NO fare (suppression, not £999,999).
_NFO_RECORD_LEN = 67  # TODO(RSPS5045 §4.13): confirm exact width.
NFO_SUPPRESSION_SENTINEL = "99999999"


def parse_nfo(line: str) -> dict[str, str]:
    """Parse a non-derivable fare override. RSPS5045 §4.13.
    NDO records take precedence over flow fares."""
    fields_ = {
        "ORIGIN_CODE":          _slice(line, 1, 4),  # TODO(§4.13): confirm offset
        "DESTINATION_CODE":     _slice(line, 5, 4),  # TODO(§4.13): confirm offset
        "ROUTE_CODE":           _slice(line, 9, 5),
        "RAILCARD_CODE":        _slice(line, 14, 3),
        "TICKET_CODE":          _slice(line, 17, 3),
        "END_DATE":             _slice(line, 20, 8),
        "START_DATE":           _slice(line, 28, 8),
        "QUOTE_DATE":           _slice(line, 36, 8),
        "SUPPRESS_MKR":         _slice(line, 44, 1),
        "ADULT_FARE":           _slice(line, 45, 8),  # 99999999 = NO fare (suppression)
        "CHILD_FARE":           _slice(line, 53, 8),  # 99999999 = NO fare
        "RESTRICTION_CODE":     _slice(line, 61, 2),
        "COMPOSITE_INDICATOR":  _slice(line, 63, 1),  # 'Y' use this, 'N' ignore
        "CROSS_LONDON_IND":     _slice(line, 64, 1),
        "PACKAGE_MKR":          _slice(line, 65, 1),
        "FARE_TRIANGLE_LOC":    _slice(line, 66, 1),
    }
    if fields_["ADULT_FARE"] == NFO_SUPPRESSION_SENTINEL:
        fields_["ADULT_FARE_NOTE"] = "SUPPRESSED (99999999 sentinel — no fare available)"
    if fields_["CHILD_FARE"] == NFO_SUPPRESSION_SENTINEL:
        fields_["CHILD_FARE_NOTE"] = "SUPPRESSED (99999999 sentinel — no fare available)"
    return fields_


# --- .TTY ticket types -----------------------------------------------------
# CLAUDE.md: TKT_CLASS (1/2/9), TKT_TYPE (S/R/N), TKT_GROUP (F/S/P/E),
# DISCOUNT_CATEGORY (links to status discount).
_TTY_RECORD_LEN = 38  # TODO(RSPS5045 §4.20): confirm exact width.


def parse_tty(line: str) -> dict[str, str]:
    """Parse a ticket-type definition. RSPS5045 §4.20."""
    return {
        "TICKET_CODE":        _slice(line, 1, 3),
        "END_DATE":           _slice(line, 4, 8),
        "START_DATE":         _slice(line, 12, 8),
        "QUOTE_DATE":         _slice(line, 20, 8),
        "DESCRIPTION":        _slice(line, 28, 15),  # TODO(§4.20): confirm offset
        "TKT_CLASS":          _slice(line, 43, 1),   # 1/2/9
        "TKT_TYPE":           _slice(line, 44, 1),   # S/R/N
        "TKT_GROUP":          _slice(line, 45, 1),   # F/S/P/E (first/std/promo/euro)
        "LAST_VALID_DATE":    _slice(line, 46, 8),
        "MAX_PASSENGERS":     _slice(line, 54, 3),
        "MIN_PASSENGERS":     _slice(line, 57, 3),
        "MAX_ADULTS":         _slice(line, 60, 3),
        "MIN_ADULTS":         _slice(line, 63, 3),
        "MAX_CHILDREN":       _slice(line, 66, 3),
        "MIN_CHILDREN":       _slice(line, 69, 3),
        "RESTRICTED_BY_DATE": _slice(line, 72, 1),
        "RESTRICTED_BY_TRAIN":_slice(line, 73, 1),
        "RESTRICTED_BY_AREA": _slice(line, 74, 1),
        "VALIDITY_CODE":      _slice(line, 75, 2),
        "ATB_DESC":           _slice(line, 77, 20),
        "TKT_TYPE_2":         _slice(line, 97, 1),
        "DISCOUNT_CATEGORY":  _slice(line, 98, 2),   # links to status discount (.DIS)
    }


# --- Dispatch --------------------------------------------------------------

ParserFn = Callable[[str], dict[str, str]]


def _ffl_dispatch(line: str) -> tuple[str, ParserFn] | tuple[None, None]:
    rt = line[:1]
    if rt == "F":
        return "FFL.F", parse_ffl_f
    if rt == "T":
        return "FFL.T", parse_ffl_t
    if rt == "R":
        # Reissue/replace marker — header-like. Skip gracefully.
        return "FFL.R", lambda l: {"RECORD_TYPE": "R", "NOTE": "reissue marker — not parsed"}
    return None, None


SUFFIX_HANDLERS: dict[str, Callable[[str], tuple[str | None, ParserFn | None]]] = {
    ".FFL": _ffl_dispatch,
    ".FSC": lambda l: ("FSC", parse_fsc),
    ".NFO": lambda l: ("NFO", parse_nfo),
    ".TTY": lambda l: ("TTY", parse_tty),
}


def inspect_lines(lines: Iterable[str], suffix: str) -> InspectResult:
    """Parse every non-comment line in `lines`. Suffix selects the handler."""
    result = InspectResult()
    handler = SUFFIX_HANDLERS.get(suffix.upper())
    if handler is None:
        raise ValueError(
            f"No handler for {suffix!r}. Supported: {sorted(SUFFIX_HANDLERS)}"
        )

    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("/"):
            continue  # comment / header / footer block per RSPS5045 §2

        try:
            kind, parser = handler(line)
        except Exception as exc:  # belt-and-braces; handlers shouldn't raise
            result.rejects.append(Reject(i, f"handler crashed: {exc!r}", line))
            continue

        if kind is None or parser is None:
            result.rejects.append(
                Reject(i, f"unrecognised RECORD_TYPE {line[:1]!r}", line)
            )
            continue

        try:
            parsed = parser(line)
        except Exception as exc:
            result.rejects.append(Reject(i, f"parse error in {kind}: {exc!r}", line))
            continue

        parsed["_KIND"] = kind
        parsed["_LINE"] = str(i)
        result.parsed.append(parsed)

    return result


def _matches(record: dict[str, str], needle: str) -> bool:
    return any(needle in v for v in record.values())


def _format(record: dict[str, str]) -> str:
    return "  ".join(f"{k}={v.strip()!r}" for k, v in record.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.ingest.inspect",
        description="Slice an RDG DTD fares-feed file into labelled fields (.FFL/.FSC/.NFO/.TTY).",
    )
    parser.add_argument("--feed", required=True, type=Path, help="Path to a feed file.")
    parser.add_argument(
        "--filter",
        dest="needle",
        default=None,
        help="Substring filter against parsed values (e.g. NLC, FLOW_ID, ticket code).",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max records to print.")
    parser.add_argument(
        "--show-rejects",
        action="store_true",
        help="Also print the quarantined malformed lines at the end.",
    )
    args = parser.parse_args(argv)

    path: Path = args.feed
    if not path.exists():
        print(f"error: feed file not found: {path}", file=sys.stderr)
        return 2

    suffix = path.suffix.upper()
    with path.open("r", encoding="latin-1") as fh:  # RDG feed is Latin-1
        result = inspect_lines(fh, suffix)

    shown = 0
    for record in result.parsed:
        if args.needle and not _matches(record, args.needle):
            continue
        print(_format(record))
        shown += 1
        if shown >= args.limit:
            break

    print(
        f"\n-- {len(result.parsed)} parsed, {len(result.rejects)} rejected, "
        f"{shown} shown (limit={args.limit})",
        file=sys.stderr,
    )

    if args.show_rejects:
        print("\n-- rejects --", file=sys.stderr)
        for r in result.rejects:
            print(f"L{r.line_no}: {r.reason} :: {r.raw!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
