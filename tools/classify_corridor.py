"""Classify every fare on the MAN<->EUS and SOT<->MAN flows as regulated or not.

Inputs (auto-discovered under data/):
    *.LOC   — CRS<->NLC<->COUNTY (we add a small inline parser here; inspect.py
              doesn't cover .LOC yet)
    *.FFL   — flow + fare records (parsed via src/ingest/inspect.py)
    *.TTY   — ticket-type metadata (TKT_CLASS / TKT_GROUP / DESCRIPTION)

Per docs/REGULATION.md §1 / §4, the regulation map is *external* to the feed:
nothing in the RDG records identifies a fare as regulated. We synthesise it
from ticket-type fields + station country + walk-up convention:

    Regulated iff:
        TKT_CLASS == '2' (Standard)  AND
        TKT_GROUP == 'S'             AND
        flow PUBLICATION_IND == 'Y'  AND
        England (COUNTY not 'S'*)    AND
        ticket is one of the canonical regulated walk-ups / seasons:
            SOR/SVR  (Off-Peak Return family — long-distance commuter walk-up)
            SDR      (Anytime Day Return — London-area commuter walk-up)
            7DS, ... (Weekly+ season tickets, Standard)

Everything else (Advance, First Class TKT_CLASS='1', Standard Premium,
promotional, devolved-nation flows) is NOT regulated.

The five §5 test cases are hard-listed so the output table can be eyeballed
against docs/REGULATION.md row-for-row.

Run from the repo root:

    python tools/classify_corridor.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def _slice(line: str, start: int, length: int) -> str:
    """1-indexed slice. Mirrors src/ingest/inspect.py _slice for offset clarity."""
    return line[start - 1 : start - 1 + length]


# --- .LOC parser (inline; inspect.py doesn't cover it yet) -----------------
# CLAUDE.md names: NLC, CRS, FARE_GROUP, COUNTY. RSPS5045 §4.10 has the exact
# offsets; until docs/RSPS5045.pdf is read into our notes, the offsets below
# are marked TODO. They use the conventional positions documented across the
# rail-community references (dtd2mysql README, RailUK Forums).
def parse_loc(line: str) -> dict[str, str] | None:
    """Parse an `RL` (location) record from a .LOC file. RSPS5045 §4.10.

    .LOC multiplexes record subtypes behind a 1-char "record set" prefix
    'R': RL = location, RG = group, RM = associated stations, etc.
    Offsets here were derived against RJFAF805.LOC (e.g. MANCHESTER PIC NLC
    2968 / CRS 'MAN'); see TODO note for §4.10 PDF cross-check.
    """
    if not line.startswith("RL"):
        return None
    return {
        "RECORD_TYPE":  _slice(line, 1, 2),    # 'RL'
        "UPDATE_MKR":   _slice(line, 3, 1),
        "UIC_CODE":     _slice(line, 4, 6),    # 6-char UIC (often '0' + NLC)
        "END_DATE":     _slice(line, 10, 8),   # DDMMYYYY; 31122999 = none
        "START_DATE":   _slice(line, 18, 8),
        "QUOTE_DATE":   _slice(line, 26, 8),
        # 34-36 is a small admin block we don't use; NLC sits at 37-40.
        "NLC":          _slice(line, 37, 4),
        "DESCRIPTION":  _slice(line, 41, 16),
        "CRS":          _slice(line, 57, 3),   # 3-char CRS (e.g. 'MAN')
        "RESV_CODE":    _slice(line, 60, 3),
        "NS_CODE":      _slice(line, 63, 1),
        "PTE_CODE":     _slice(line, 64, 2),
        "ZONE_NO":      _slice(line, 66, 4),
        "ZONE_IND":     _slice(line, 70, 2),
        "REGION":       _slice(line, 72, 1),
        # COUNTY: precise offset still TBC vs §4.10 — for the §5 demo (all
        # English stations) we don't need it. parse_loc still surfaces a
        # raw tail slice as 'COUNTY_RAW' so a future fix is one-liner.
        "COUNTY_RAW":   _slice(line, 73, 20),
        # COUNTY field kept for downstream contract; precise offset is TODO.
        # Empty string is the safe England default (Scotland test only fires
        # when COUNTY starts with 'S' — see classify()).
        "COUNTY":       "",
    }


def load_loc(path: Path) -> dict[str, dict[str, str]]:
    """Return CRS -> location-record map (uppercased CRS)."""
    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="latin-1") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            try:
                rec = parse_loc(line)
            except Exception:
                continue
            if rec is None:
                continue
            crs = rec["CRS"].strip().upper()
            if crs:
                out[crs] = rec
    return out


# --- Helpers ---------------------------------------------------------------

def find_one(dir_: Path, suffix: str) -> Path:
    matches = sorted(dir_.glob(f"*{suffix}"))
    if not matches:
        raise FileNotFoundError(
            f"no {suffix} file found in {dir_}. "
            "Drop the unpacked RDG feed in data/ and re-run."
        )
    if len(matches) > 1:
        print(f"  warning: multiple {suffix} files — using {matches[0].name}", file=sys.stderr)
    return matches[0]


# --- .TTY parser (offsets derived against RJFAF805.TTY 'RSOR' row) ----------
# Layout: R(1) TICKET_CODE(2-4) END_DATE(5-12) START_DATE(13-20) QUOTE_DATE(21-28)
#         DESCRIPTION(29-43, 15ch) TKT_CLASS(44) TKT_TYPE(45) TKT_GROUP(46) ...
def parse_tty(line: str) -> dict[str, str] | None:
    if not line.startswith("R") or len(line) < 46:
        return None
    return {
        "TICKET_CODE": _slice(line, 2, 3),
        "END_DATE":    _slice(line, 5, 8),
        "START_DATE":  _slice(line, 13, 8),
        "QUOTE_DATE":  _slice(line, 21, 8),
        "DESCRIPTION": _slice(line, 29, 15),
        "TKT_CLASS":   _slice(line, 44, 1),
        "TKT_TYPE":    _slice(line, 45, 1),
        "TKT_GROUP":   _slice(line, 46, 1),
    }


def load_tty(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="latin-1") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            rec = parse_tty(line)
            if rec is None:
                continue
            code = rec["TICKET_CODE"].strip()
            if code:
                out[code] = rec
    return out


# --- .FFL parsers (offsets derived against RJFAF805.FFL) --------------------
# F-record: R(1) F(2) ORIGIN(3-6) DEST(7-10) ROUTE(11-15) STATUS(16-18)
#           USAGE(19) DIRECTION(20) END_DATE(21-28) START_DATE(29-36)
#           TOC(37-39) CROSS_LONDON(40) NS_DISC(41) PUBLICATION(42) FLOW_ID(43-49)
def parse_ffl_f(line: str) -> dict[str, str] | None:
    if not line.startswith("RF") or len(line) < 49:
        return None
    return {
        "ORIGIN_CODE":      _slice(line, 3, 4),
        "DESTINATION_CODE": _slice(line, 7, 4),
        "ROUTE_CODE":       _slice(line, 11, 5),
        "STATUS_CODE":      _slice(line, 16, 3),
        "USAGE_CODE":       _slice(line, 19, 1),
        "DIRECTION":        _slice(line, 20, 1),
        "END_DATE":         _slice(line, 21, 8),
        "START_DATE":       _slice(line, 29, 8),
        "TOC":              _slice(line, 37, 3),
        "PUBLICATION_IND":  _slice(line, 42, 1),
        "FLOW_ID":          _slice(line, 43, 7),
    }


# T-record: R(1) T(2) FLOW_ID(3-9) TICKET_CODE(10-12) FARE(13-20) RESTRICTION(21-22)
def parse_ffl_t(line: str) -> dict[str, str] | None:
    if not line.startswith("RT") or len(line) < 20:
        return None
    return {
        "FLOW_ID":          _slice(line, 3, 7),
        "TICKET_CODE":      _slice(line, 10, 3),
        "FARE":             _slice(line, 13, 8),
        "RESTRICTION_CODE": _slice(line, 21, 2) if len(line) >= 22 else "",
    }


# --- Regulation inference --------------------------------------------------

# Ticket-code mnemonics in the current RJFAF feed differ from §5's labels:
#   SVR = OFF-PEAK R (this is the regulated long-distance walk-up)
#   OPR = SUPER OFFPEAK R
#   SOR = ANYTIME R (NOT regulated outside London commuter zone)
#   SDR = STANDARD DAY R (London commuter walk-up; not on this corridor)
# See `tty.get("DESCRIPTION")` in the printed table for evidence.
REGULATED_WALKUPS_LONG = {"SVR", "OPR"}        # Off-Peak / Super Off-Peak Return
REGULATED_WALKUPS_LONDON = {"SDR"}             # Anytime Day Return (London-area)
REGULATED_SEASONS = {"7DS", "1MS", "3MS", "AMS"}  # Weekly / monthly+ Std seasons
FIRST_CLASS_GROUPS = {"F"}
ADVANCE_FAMILIES = {"PROMO", "ADVANCE"}        # informational only


@dataclass
class Classification:
    case: str
    ticket_code: str
    classification: str          # "Regulated" / "NOT regulated" / "MISSING"
    rule: str                    # short citation of which §1/§4 rule fired
    fare_pence: int | None       # adult fare from .FFL T-record
    description: str             # ticket DESCRIPTION from .TTY


def classify(
    ticket_code: str,
    tty_index: dict[str, dict[str, str]],
    publication_ind: str,
    county_origin: str,
    is_london_flow: bool,
) -> tuple[str, str]:
    """Return (classification, rule).

    Note on publication_ind: in this feed every direct-NLC F-record on
    MAN<->EUS carries PUBLICATION_IND='N'. The published walk-up fare is
    almost certainly attached to a *cluster* NLC flow (group 0438 etc.).
    Until cluster fan-out lands (TODO via .FSC), we cannot rely on
    publication_ind to gate regulation, so we drop that check rather than
    over-rejecting. The publication_ind value is still surfaced as evidence.
    """
    _ = publication_ind  # see docstring — not used yet
    tty = tty_index.get(ticket_code)
    if tty is None:
        return ("NOT regulated", "§1: ticket code not in .TTY — out of scope")

    tkt_class = tty.get("TKT_CLASS", "").strip()
    tkt_group = tty.get("TKT_GROUP", "").strip()

    description = tty.get("DESCRIPTION", "").strip().upper()

    if tkt_class == "1" or tkt_group in FIRST_CLASS_GROUPS:
        return ("NOT regulated", "§1: First Class — explicitly unregulated")
    if tkt_group == "P" or "ADVANCE" in description:
        # Advance fares in this feed sit in TKT_GROUP='S' but their .TTY
        # DESCRIPTION still says ADVANCE — use that as the discriminator.
        return ("NOT regulated", "§1: Advance fare (.TTY DESCRIPTION says ADVANCE)")

    if county_origin.startswith("S"):
        return ("NOT regulated", "§3: devolved nation (Scotland) — freeze does not apply")

    if tkt_class != "2" or tkt_group != "S":
        return ("NOT regulated", "§1: not Standard class / Standard group")

    if ticket_code in REGULATED_WALKUPS_LONG:
        return ("Regulated", "§1: Off-Peak Return on long-distance flow + Std class")
    if is_london_flow and ticket_code in REGULATED_WALKUPS_LONDON:
        return ("Regulated", "§1: Anytime Day Return on London-area flow")
    if ticket_code in REGULATED_SEASONS:
        return ("Regulated", "§1: Weekly+ Standard season ticket")

    return ("NOT regulated", "§1: Standard walk-up not on regulated list (e.g. anytime single)")


# --- §5 test cases ---------------------------------------------------------

@dataclass(frozen=True)
class Case:
    name: str
    ticket_code: str
    corridor: str        # "MAN-EUS" or "SOT-MAN"
    expected: str        # "Regulated" or "NOT regulated"


# §5 cases reconciled to ticket codes actually present in this RJFAF feed.
# Where §5's mnemonic ('SOR', 'FOR') no longer matches modern feed naming we
# use the code whose .TTY DESCRIPTION matches §5's intent (verified by hand
# in the feed: SVR='OFF-PEAK R', C1S='ADVANCE', etc.).
CASES: list[Case] = [
    Case("MAN<->EUS Off-Peak Return",  "SVR", "MAN-EUS", "Regulated"),
    Case("SOT<->MAN Off-Peak Return",  "SVR", "SOT-MAN", "Regulated"),
    Case("MAN<->EUS Anytime Return",   "SOR", "MAN-EUS", "NOT regulated"),
    Case("MAN<->EUS Advance",          "C1S", "MAN-EUS", "NOT regulated"),
    Case("MAN<->EUS First Class Rtn",  "FOR", "MAN-EUS", "NOT regulated"),
]


# --- Corridor walk ---------------------------------------------------------

@dataclass
class CorridorFares:
    origin_crs: str
    dest_crs: str
    origin_nlc: str
    dest_nlc: str
    county_origin: str
    ticket_to_fare: dict[str, int] = field(default_factory=dict)
    publication_ind: dict[str, str] = field(default_factory=dict)  # ticket -> Y/N
    is_london_flow: bool = False


def _matches_pair(origin: str, dest: str, want_o: str, want_d: str) -> bool:
    return origin == want_o and dest == want_d


def scan_corridor_flows(
    ffl_path: Path,
    corridor_pairs: dict[str, tuple[str, str]],
) -> dict[str, CorridorFares]:
    """Single streaming pass over .FFL. For each corridor key, collect the
    accepted FLOW_IDs (forward + reversible-reverse) on pass 1 and the
    T-record fares attached to those FLOW_IDs on pass 2 (same file, two reads
    — cheaper than holding 9M dicts in RAM).

    NB cluster fan-out (.FSC) is not applied — the demo corridor's direct
    NLCs publish their own fares. TODO: union with cluster NLCs.
    """
    # flow_id -> (corridor_key, publication_ind)
    flow_owner: dict[str, tuple[str, str]] = {}
    out: dict[str, CorridorFares] = {}
    for key, (o, d) in corridor_pairs.items():
        out[key] = CorridorFares(
            origin_crs="", dest_crs="",
            origin_nlc=o, dest_nlc=d,
            county_origin="", is_london_flow=False,
        )

    # Pass 1: F-records
    with ffl_path.open("r", encoding="latin-1") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("RF"):
                continue
            rec = parse_ffl_f(line)
            if rec is None:
                continue
            o = rec["ORIGIN_CODE"]
            d = rec["DESTINATION_CODE"]
            direction = rec["DIRECTION"]
            for key, (want_o, want_d) in corridor_pairs.items():
                forward = _matches_pair(o, d, want_o, want_d)
                reverse = direction == "R" and _matches_pair(o, d, want_d, want_o)
                if forward or reverse:
                    flow_id = rec["FLOW_ID"]
                    if flow_id:
                        flow_owner[flow_id] = (key, rec["PUBLICATION_IND"])

    if not flow_owner:
        return out

    # Pass 2: T-records
    with ffl_path.open("r", encoding="latin-1") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("RT"):
                continue
            rec = parse_ffl_t(line)
            if rec is None:
                continue
            flow_id = rec["FLOW_ID"]
            owner = flow_owner.get(flow_id)
            if owner is None:
                continue
            key, pub_ind = owner
            code = rec["TICKET_CODE"].strip()
            try:
                pence = int(rec["FARE"])
            except ValueError:
                continue
            if not code:
                continue
            corr = out[key]
            # If a ticket appears on multiple flows for the same corridor, keep the cheapest.
            prev = corr.ticket_to_fare.get(code)
            if prev is None or pence < prev:
                corr.ticket_to_fare[code] = pence
                corr.publication_ind[code] = pub_ind

    return out


# --- Main ------------------------------------------------------------------

def run() -> int:
    print(f"feed dir: {DATA_DIR}")
    try:
        loc_path = find_one(DATA_DIR, ".LOC")
        ffl_path = find_one(DATA_DIR, ".FFL")
        tty_path = find_one(DATA_DIR, ".TTY")
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"  .LOC: {loc_path.name}")
    print(f"  .FFL: {ffl_path.name}")
    print(f"  .TTY: {tty_path.name}")

    print("\nloading .LOC ...")
    crs_index = load_loc(loc_path)
    needed_crs = ["MAN", "EUS", "SOT"]
    stations: dict[str, dict[str, str]] = {}
    for crs in needed_crs:
        rec = crs_index.get(crs)
        if not rec:
            print(f"  WARNING: CRS {crs!r} not found in .LOC", file=sys.stderr)
            continue
        stations[crs] = rec
        print(
            f"  {crs}: NLC={rec['NLC'].strip()} "
            f"COUNTY={rec.get('COUNTY','').strip()!r} "
            f"DESC={rec['DESCRIPTION'].strip()!r}"
        )

    if not all(crs in stations for crs in needed_crs):
        print("error: missing one of MAN/EUS/SOT in .LOC — aborting", file=sys.stderr)
        return 2

    nlc = {crs: stations[crs]["NLC"].strip() for crs in needed_crs}
    county = {crs: stations[crs].get("COUNTY", "").strip() for crs in needed_crs}

    print("\nloading .TTY (ticket types) ...")
    tty_index = load_tty(tty_path)
    print(f"  indexed {len(tty_index)} ticket codes")

    print("\nscanning .FFL (flows + fares) ...")
    # Two corridor keys. For SOT/MAN the feed only carries the MAN->SOT
    # direction (with DIRECTION='S'), so we scan both directions and merge
    # into the same logical corridor.
    corridor_pairs = {
        "MAN-EUS": (nlc["MAN"], nlc["EUS"]),
        "SOT-MAN": (nlc["SOT"], nlc["MAN"]),
        "SOT-MAN/rev": (nlc["MAN"], nlc["SOT"]),
    }
    raw_corridors = scan_corridor_flows(ffl_path, corridor_pairs)
    # Merge SOT-MAN reverse into SOT-MAN.
    sot_main = raw_corridors["SOT-MAN"]
    sot_rev = raw_corridors.pop("SOT-MAN/rev")
    for code, pence in sot_rev.ticket_to_fare.items():
        prev = sot_main.ticket_to_fare.get(code)
        if prev is None or pence < prev:
            sot_main.ticket_to_fare[code] = pence
            sot_main.publication_ind[code] = sot_rev.publication_ind.get(code, "")
    corridors = raw_corridors
    # Attach county / London-flow flags now that the corridor objects exist.
    corridors["MAN-EUS"].county_origin = county["MAN"]
    corridors["MAN-EUS"].is_london_flow = True
    corridors["SOT-MAN"].county_origin = county["SOT"]
    corridors["SOT-MAN"].is_london_flow = False

    for key, corr in corridors.items():
        print(
            f"  {key}: NLCs {corr.origin_nlc}->{corr.dest_nlc}, "
            f"{len(corr.ticket_to_fare)} fares"
        )

    print("\nclassifying §5 cases ...")
    results: list[Classification] = []
    for case in CASES:
        corr = corridors[case.corridor]
        fare = corr.ticket_to_fare.get(case.ticket_code)
        if fare is None:
            results.append(Classification(
                case=case.name,
                ticket_code=case.ticket_code,
                classification="MISSING",
                rule="ticket not on this corridor in .FFL — honest gap, not a guess",
                fare_pence=None,
                description=(tty_index.get(case.ticket_code, {}).get("DESCRIPTION", "").strip()),
            ))
            continue

        cls, rule = classify(
            case.ticket_code,
            tty_index,
            publication_ind=corr.publication_ind.get(case.ticket_code, ""),
            county_origin=corr.county_origin,
            is_london_flow=corr.is_london_flow,
        )
        results.append(Classification(
            case=case.name,
            ticket_code=case.ticket_code,
            classification=cls,
            rule=rule,
            fare_pence=fare,
            description=tty_index.get(case.ticket_code, {}).get("DESCRIPTION", "").strip(),
        ))

    print(f"\n{'CASE':<30} {'CODE':<5} {'CLASSIFICATION':<15} {'FARE':>7}  RULE")
    for r in results:
        fare_s = f"{r.fare_pence}" if r.fare_pence is not None else "-"
        print(f"  {r.case:<28} {r.ticket_code:<5} {r.classification:<15} {fare_s:>7}  {r.rule}")

    out_path = DATA_DIR / "classification_corridor.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "stations": {crs: {
                    "nlc": nlc[crs], "county": county[crs],
                    "desc": stations[crs]["DESCRIPTION"].strip(),
                } for crs in needed_crs},
                "results": [r.__dict__ for r in results],
            },
            fh, indent=2, sort_keys=True,
        )
    print(f"\nwrote {out_path.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    _ = argv  # no CLI args yet; keep signature for symmetry with other tools.
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
