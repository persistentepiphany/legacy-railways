"""Feed inspector: slice fixed-width RDG DTD records into labelled fields.

This is the "see-the-mess" tool. It opens an RDG DTD fares feed file
(`.FFL`, `.FSC`, `.NFO`, `.TTY`, `.LOC`), walks each fixed-position record,
prints the named fields, and quarantines malformed records into a rejects
list without crashing. dtd2mysql crashes on bad records; we don't.

Usage:
    python -m src.ingest.inspect --feed path/to/RJFAF.FFL [--filter STR]
                                  [--limit N] [--show-rejects]

The `--filter` is a plain substring match against the parsed field values
(e.g. an NLC like "0438", a FLOW_ID, or a ticket code like "SOR").

Offsets were verified empirically against the RJFAF805 snapshot:
the F-record's FLOW_ID at pos 43-49 cross-checks with T-records starting
`RT<flow_id>`. Where the layout still needs the RSPS5045 PDF to confirm
secondary fields, it's marked `TODO(RSPS5045 §X.Y)`.

Note on the leading 'R' prefix: every record on disk begins with 'R' as a
universal "row" prefix. The actual record type ('F', 'T', 'L', 'G', ...) is
at position 2. The dispatch and the parsed RECORD_TYPE field both read
position 2 accordingly.

This module also exposes two helpers used by `src.resolver.resolve`:
    find_flows(feed_path, origin_nlc, dest_nlc) -> list[FlowRecord]
    find_fares(feed_path, flow_id)              -> list[FareRecord]
Both attach the source line number so callers can build provenance.
"""

from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar


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
# Every line begins with 'R' as a universal prefix; the record type is at
# position 2 ('F' for flow header, 'T' for fare row). Layout verified
# empirically against RJFAF805 — the first F-record's FLOW_ID at pos 43-49
# = '0000020' matches `grep ^RT0000020 …FFL` exactly. RSPS5045 §4.4.
_FFL_F_MIN_LEN = 49   # 'RF' + 4+4+5+3+1+1+8+8+3+1+1+1+7 = 49; on-disk pad to 50.
_FFL_T_MIN_LEN = 20   # 'RT' + 7+3+8 = 20; restriction code at 21-22 is optional.


def parse_ffl_f(line: str) -> dict[str, str]:
    """Parse an `F` (flow header) record from a .FFL file. RSPS5045 §4.4."""
    return {
        "RECORD_TYPE":     _slice(line, 2, 1),       # 'F' (pos 1 is the 'R' prefix)
        "ORIGIN_CODE":     _slice(line, 3, 4),       # NLC
        "DESTINATION_CODE":_slice(line, 7, 4),       # NLC
        "ROUTE_CODE":      _slice(line, 11, 5),
        "STATUS_CODE":     _slice(line, 16, 3),
        "USAGE_CODE":      _slice(line, 19, 1),      # 'A'=actual, 'G'=generated
        "DIRECTION":       _slice(line, 20, 1),      # 'R'=reversible, 'S'=single-direction
        "END_DATE":        _slice(line, 21, 8),      # 31122999 = no end
        "START_DATE":      _slice(line, 29, 8),
        "TOC":             _slice(line, 37, 3),
        "CROSS_LONDON_IND":_slice(line, 40, 1),
        "NS_DISC_IND":     _slice(line, 41, 1),
        "PUBLICATION_IND": _slice(line, 42, 1),      # 'Y' = published; used by regulation map
        "FLOW_ID":         _slice(line, 43, 7),
    }


def parse_ffl_t(line: str) -> dict[str, str]:
    """Parse a `T` (fare) record from a .FFL file. RSPS5045 §4.4.
    Linked to its F-record parent by FLOW_ID."""
    return {
        "RECORD_TYPE":      _slice(line, 2, 1),      # 'T' (pos 1 is the 'R' prefix)
        "FLOW_ID":          _slice(line, 3, 7),      # joins back to F
        "TICKET_CODE":      _slice(line, 10, 3),
        "FARE":             _slice(line, 13, 8),     # pence
        "RESTRICTION_CODE": _slice(line, 21, 2),
    }


# --- .FSC station cluster --------------------------------------------------
# Layout verified empirically against RJFAF805 (sample 'RLS5729683112299923042026'):
#   pos 1     'R'         universal record prefix
#   pos 2-5   CLUSTER_ID  alphanumeric (e.g. 'LS57', 'C552', '2222')
#   pos 6-9   MEMBER_NLC  the station NLC inside the cluster
#   pos 10-17 END_DATE    DDMMYYYY (31122999 = no end)
#   pos 18-25 START_DATE  DDMMYYYY
# Note: the apparent 'RA'/'RQ'/'RT'/... 2-char prefix is actually 'R' + the
# first letter of the cluster_id, NOT a record-subtype field.
# Verified by cross-reference: FFL flow `RF1444LS57...` (route 01491 LUMO)
# corresponds to FSC member relationship `RLS5729680000...` (LS57 contains 2968).
_FSC_RECORD_LEN = 25  # RSPS5045 §4.18 (offsets confirmed empirically).


def parse_fsc(line: str) -> dict[str, str]:
    """Parse a station-cluster row. RSPS5045 §4.18.
    One CLUSTER_ID governs many MEMBER_NLCs — a fare flow set on CLUSTER_ID
    applies to every member, the source of blast-radius fan-out."""
    return {
        "RECORD_TYPE":  _slice(line, 1, 1),       # 'R' prefix only
        "CLUSTER_ID":   _slice(line, 2, 4),       # may be alphanumeric ('LS57', 'C552')
        "MEMBER_NLC":   _slice(line, 6, 4),       # the station NLC inside the cluster
        "END_DATE":     _slice(line, 10, 8),      # DDMMYYYY (31122999 = no end)
        "START_DATE":   _slice(line, 18, 8),
    }


# --- .NFO non-derivable overrides ------------------------------------------
# CLAUDE.md: COMPOSITE_INDICATOR 'Y'=use this record / 'N'=ignore;
# ADULT_FARE/CHILD_FARE = 99999999 means NO fare (suppression, not £999,999).
# Layout verified against RJFAF805 row
#   R0027003401000   ADTO311229992903202607012025N0000166000000830  YNN
# (67 chars). Note the single-char field at pos 21 (always 'O' in sampled
# rows — likely a fare-class / "outward only" flag; surfaced as MARKER).
_NFO_RECORD_LEN = 67
NFO_SUPPRESSION_SENTINEL = "99999999"


def parse_nfo(line: str) -> dict[str, str]:
    """Parse a non-derivable fare override. RSPS5045 §4.13.
    NDO records take precedence over flow fares."""
    fields_ = {
        "RECORD_TYPE":          _slice(line, 1, 1),  # 'R' file prefix
        "ORIGIN_CODE":          _slice(line, 2, 4),
        "DESTINATION_CODE":     _slice(line, 6, 4),
        "ROUTE_CODE":           _slice(line, 10, 5),
        "RAILCARD_CODE":        _slice(line, 15, 3),
        "TICKET_CODE":          _slice(line, 18, 3),
        "MARKER":               _slice(line, 21, 1),  # TODO(§4.13): 'O' in sampled rows
        "END_DATE":             _slice(line, 22, 8),  # 31122999 = none
        "START_DATE":           _slice(line, 30, 8),
        "QUOTE_DATE":           _slice(line, 38, 8),
        "SUPPRESS_MKR":         _slice(line, 46, 1),
        "ADULT_FARE":           _slice(line, 47, 8),  # 99999999 = NO fare (suppression)
        "CHILD_FARE":           _slice(line, 55, 8),  # 99999999 = NO fare
        "RESTRICTION_CODE":     _slice(line, 63, 2),
        "COMPOSITE_INDICATOR":  _slice(line, 65, 1),  # 'Y' use this, 'N' ignore
        "CROSS_LONDON_IND":     _slice(line, 66, 1),
        "PACKAGE_MKR":          _slice(line, 67, 1),
    }
    if fields_["ADULT_FARE"] == NFO_SUPPRESSION_SENTINEL:
        fields_["ADULT_FARE_NOTE"] = "SUPPRESSED (99999999 sentinel — no fare available)"
    if fields_["CHILD_FARE"] == NFO_SUPPRESSION_SENTINEL:
        fields_["CHILD_FARE_NOTE"] = "SUPPRESSED (99999999 sentinel — no fare available)"
    return fields_


# --- .TTY ticket types -----------------------------------------------------
# CLAUDE.md: TKT_CLASS (1/2/9), TKT_TYPE (S/R/N), TKT_GROUP (F/S/P/E),
# DISCOUNT_CATEGORY (links to status discount).
# Layout per RSPS5045 §4.6. Position 1 in the spec is UPDATE_MARKER (always 'R'
# in full-file refresh dumps); historically we treated this as a universal
# 'R' prefix but it's actually the per-record update marker. The interpretation
# difference doesn't change offsets — TICKET_CODE is still at 2-4 — but it
# matters when reading the spec.
# Verified against RJFAF805 row
#   RSOR311229992305201722052017ANYTIME R      2RS31122999001001001000001000NNN41...
_TTY_RECORD_LEN = 113  # need DISCOUNT_CATEGORY at 112-113. Full record is ~113 chars.


def parse_tty(line: str) -> dict[str, str]:
    """Parse a ticket-type definition. RSPS5045 §4.6 — full layout.
    DISCOUNT_CATEGORY at pos 112-113 is the link to the .DIS status-discount
    record (resolver railcard chain depends on it; do NOT move)."""
    return {
        "UPDATE_MARKER":      _slice(line, 1, 1),    # I/A/D/R; 'R' = full refresh
        "TICKET_CODE":        _slice(line, 2, 3),
        "END_DATE":           _slice(line, 5, 8),
        "START_DATE":         _slice(line, 13, 8),
        "QUOTE_DATE":         _slice(line, 21, 8),
        "DESCRIPTION":        _slice(line, 29, 15),
        "TKT_CLASS":          _slice(line, 44, 1),   # 1/2/9
        "TKT_TYPE":           _slice(line, 45, 1),   # S/R/N
        "TKT_GROUP":          _slice(line, 46, 1),   # F/S/P/E
        "LAST_VALID_DAY":     _slice(line, 47, 8),
        "MAX_PASSENGERS":     _slice(line, 55, 3),
        "MIN_PASSENGERS":     _slice(line, 58, 3),
        "MAX_ADULTS":         _slice(line, 61, 3),
        "MIN_ADULTS":         _slice(line, 64, 3),
        "MAX_CHILDREN":       _slice(line, 67, 3),
        "MIN_CHILDREN":       _slice(line, 70, 3),
        "RESTRICTED_BY_DATE": _slice(line, 73, 1),
        "RESTRICTED_BY_TRAIN":_slice(line, 74, 1),
        "RESTRICTED_BY_AREA": _slice(line, 75, 1),
        "VALIDITY_CODE":      _slice(line, 76, 2),
        "ATB_DESCRIPTION":    _slice(line, 78, 20),
        "LUL_XLONDON_ISSUE":  _slice(line, 98, 1),
        "RESERVATION_REQUIRED": _slice(line, 99, 1),
        "CAPRI_CODE":         _slice(line, 100, 3),
        "LUL_93":             _slice(line, 103, 1),
        "UTS_CODE":           _slice(line, 104, 2),
        "TIME_RESTRICTION":   _slice(line, 106, 1),
        "FREE_PASS_LUL":      _slice(line, 107, 1),
        "PACKAGE_MKR":        _slice(line, 108, 1),
        "FARE_MULTIPLIER":    _slice(line, 109, 3),
        "DISCOUNT_CATEGORY":  _slice(line, 112, 2),  # links to status discount (.DIS)
    }


# --- .RLC railcards (RSPS5045 §4.15) ---------------------------------------
# 127-char fixed-width row, no leading prefix (RAILCARD_CODE at pos 1-3).
# Verified against YNG row in RJFAF805.RLC: ADULT_STATUS at pos 119-121 = "003"
# — that's the link that drives the railcard discount chain. The first row in
# the file is "   " (3 spaces) = the "no railcard" record (per §4.15.2 field 1).
_RLC_RECORD_LEN = 127


def parse_rlc(line: str) -> dict[str, str]:
    """Parse a railcard record. RSPS5045 §4.15.2.
    ADULT_STATUS (pos 119-121) is the key field for the resolver — it links
    the railcard to the status-discount record in .DIS."""
    return {
        "RAILCARD_CODE":        _slice(line, 1, 3),    # "   " = no railcard
        "END_DATE":             _slice(line, 4, 8),
        "START_DATE":           _slice(line, 12, 8),
        "QUOTE_DATE":           _slice(line, 20, 8),
        "HOLDER_TYPE":          _slice(line, 28, 1),   # 'A' adult, 'C' child
        "DESCRIPTION":          _slice(line, 29, 20),
        "RESTRICTED_BY_ISSUE":  _slice(line, 49, 1),
        "RESTRICTED_BY_AREA":   _slice(line, 50, 1),
        "RESTRICTED_BY_TRAIN":  _slice(line, 51, 1),
        "RESTRICTED_BY_DATE":   _slice(line, 52, 1),
        "MASTER_CODE":          _slice(line, 53, 3),
        "DISPLAY_FLAG":         _slice(line, 56, 1),
        "MAX_PASSENGERS":       _slice(line, 57, 3),
        "MIN_PASSENGERS":       _slice(line, 60, 3),
        "MAX_HOLDERS":          _slice(line, 63, 3),
        "MIN_HOLDERS":          _slice(line, 66, 3),
        "MAX_ACC_ADULTS":       _slice(line, 69, 3),
        "MIN_ACC_ADULTS":       _slice(line, 72, 3),
        "MAX_ADULTS":           _slice(line, 75, 3),
        "MIN_ADULTS":           _slice(line, 78, 3),
        "MAX_CHILDREN":         _slice(line, 81, 3),
        "MIN_CHILDREN":         _slice(line, 84, 3),
        "PRICE":                _slice(line, 87, 8),
        "DISCOUNT_PRICE":       _slice(line, 95, 8),
        "VALIDITY_PERIOD":      _slice(line, 103, 4),
        "LAST_VALID_DATE":      _slice(line, 107, 8),
        "PHYSICAL_CARD":        _slice(line, 115, 1),
        "CAPRI_TICKET_TYPE":    _slice(line, 116, 3),
        "ADULT_STATUS":         _slice(line, 119, 3),
        "CHILD_STATUS":         _slice(line, 122, 3),
        "AAA_STATUS":           _slice(line, 125, 3),
    }


# --- .DIS status discounts (RSPS5045 §4.17) --------------------------------
# Two record types, distinguished by the first character:
#   'S' (100 chars): Status record — holds flat-fare maxes and lower/higher mins
#   'D' (18  chars): Status Discount record — keyed by (status, category)
# Verified against RJFAF805.DIS: D003 STATUS=003 CAT=01 IND=0 PCT=334 (33.4% off).
_DIS_S_RECORD_LEN = 99
_DIS_D_RECORD_LEN = 18


def parse_dis_status(line: str) -> dict[str, str]:
    """Parse a Status (`S`) record. RSPS5045 §4.17.2.
    Holds the per-status flat fares and lower/higher minimum-fare caps
    referenced by the 'F'/'M'/'H'/'L' DISCOUNT_INDICATOR values."""
    return {
        "RECORD_TYPE":           _slice(line, 1, 1),    # 'S'
        "STATUS_CODE":           _slice(line, 2, 3),
        "END_DATE":              _slice(line, 5, 8),
        "START_DATE":            _slice(line, 13, 8),
        "ATB_DESC":              _slice(line, 21, 5),
        "CC_DESC":               _slice(line, 26, 5),
        "UTS_CODE":              _slice(line, 31, 1),
        "FIRST_SINGLE_MAX_FLAT": _slice(line, 32, 8),
        "FIRST_RETURN_MAX_FLAT": _slice(line, 40, 8),
        "STD_SINGLE_MAX_FLAT":   _slice(line, 48, 8),
        "STD_RETURN_MAX_FLAT":   _slice(line, 56, 8),
        "FIRST_LOWER_MIN":       _slice(line, 64, 8),
        "FIRST_HIGHER_MIN":      _slice(line, 72, 8),
        "STD_LOWER_MIN":         _slice(line, 80, 8),
        "STD_HIGHER_MIN":        _slice(line, 88, 8),
        "FS_MKR":                _slice(line, 96, 1),
        "FR_MKR":                _slice(line, 97, 1),
        "SS_MKR":                _slice(line, 98, 1),
        "SR_MKR":                _slice(line, 99, 1),
    }


def parse_dis_discount(line: str) -> dict[str, str]:
    """Parse a Status Discount (`D`) record. RSPS5045 §4.17.3.
    Linked to a Status record by (STATUS_CODE, END_DATE). DISCOUNT_INDICATOR
    values: '0' = pct in DISCOUNT_PERCENTAGE; 'F' = use status flat fare;
    'M'/'H'/'L' = pct with max/higher-min/lower-min caps; 'X'/'N' = no discount.
    DISCOUNT_PERCENTAGE is to one decimal place — 334 = 33.4%."""
    return {
        "RECORD_TYPE":         _slice(line, 1, 1),     # 'D'
        "STATUS_CODE":         _slice(line, 2, 3),
        "END_DATE":            _slice(line, 5, 8),
        "DISCOUNT_CATEGORY":   _slice(line, 13, 2),    # 01-20
        "DISCOUNT_INDICATOR":  _slice(line, 15, 1),    # 0/F/M/H/L/X/N
        "DISCOUNT_PERCENTAGE": _slice(line, 16, 3),    # 334 = 33.4%
    }


# --- .RCM railcard minimum fares (RSPS5045 §4.16) --------------------------
# 30-char fixed-width row, keyed by (RAILCARD_CODE, TICKET_CODE).
# Applies to adult fares only (per §4.16.1.1).
_RCM_RECORD_LEN = 30


def parse_rcm(line: str) -> dict[str, str]:
    """Parse a railcard minimum-fare record. RSPS5045 §4.16.2."""
    return {
        "RAILCARD_CODE": _slice(line, 1, 3),
        "TICKET_CODE":   _slice(line, 4, 3),
        "END_DATE":      _slice(line, 7, 8),
        "START_DATE":    _slice(line, 15, 8),
        "MINIMUM_FARE":  _slice(line, 23, 8),   # pence
    }


# --- .FRR rounding rules (RSPS5045 §4.18) ----------------------------------
# 36-char fixed-width row. For a discounted fare, walk the bands of the
# selected RULE_NO in ascending RULE_INDEX order and pick the first whose
# MAX_AMOUNT >= fare; round UP to that ROUND_AMOUNT.
_FRR_RECORD_LEN = 36


def parse_frr(line: str) -> dict[str, str]:
    """Parse a rounding-rule record. RSPS5045 §4.18.2."""
    return {
        "RULE_NO":      _slice(line, 1, 2),
        "END_DATE":     _slice(line, 3, 8),
        "RULE_INDEX":   _slice(line, 11, 2),
        "START_DATE":   _slice(line, 13, 8),
        "MAX_AMOUNT":   _slice(line, 21, 8),   # pence; 99999999 = high
        "ROUND_AMOUNT": _slice(line, 29, 8),   # pence; round UP to this
    }


# --- .LOC location ----------------------------------------------------------
# Offsets verified empirically against RJFAF805 for NLC 0438 (MANCHESTER STNS)
# and NLC 1444 (LONDON EUSTON). RSPS5045 §4.10. Only 'RL' subtype-'0' records
# are parsed; 'RG' (group) and other subtypes are recognised but quarantined
# until we need them (cluster expansion is a deferred slice).
_LOC_MIN_LEN = 80  # need at least NLC..COUNTY; full record is ~289 chars.


def parse_loc(line: str) -> dict[str, str]:
    """Parse an `L` (location) record from a .LOC file. RSPS5045 §4.10.
    NLC at 5-8, station name at 41-56, CRS at 57-59, GROUP_NLC at 70-73,
    COUNTY at 76-77 — verified against MANCHESTER STNS (0438) and
    LONDON EUSTON (1444). Other fields are TODO until pinned from the PDF."""
    return {
        "RECORD_TYPE":  _slice(line, 2, 1),       # 'L'
        "SUBTYPE":      _slice(line, 4, 1),       # '0' for the active location row
        "NLC":          _slice(line, 5, 4),       # the 4-char fares NLC
        "END_DATE":     _slice(line, 10, 8),      # TODO(§4.10): confirm date layout
        "START_DATE":   _slice(line, 18, 8),      # TODO(§4.10)
        "QUOTE_DATE":   _slice(line, 26, 8),      # TODO(§4.10)
        "NLC_KEY":      _slice(line, 37, 4),      # duplicate NLC / station key
        "STATION_NAME": _slice(line, 41, 16),     # 16-char space-padded name
        "CRS":          _slice(line, 57, 3),      # blank for group stations
        "FARE_GROUP":   _slice(line, 60, 5),
        "GROUP_NLC":    _slice(line, 70, 4),      # for members, the group they belong to
        "COUNTY":       _slice(line, 76, 2),      # decides England/Scotland for regulation
    }


# --- Dispatch --------------------------------------------------------------

ParserFn = Callable[[str], dict[str, str]]


def _check_len(line: str, min_len: int, kind: str) -> str | None:
    """Return None if OK, else a reject-reason string."""
    if len(line) < min_len:
        return f"line too short for {kind}: got {len(line)} chars, need {min_len}"
    return None


def _ffl_dispatch(line: str) -> tuple[str, ParserFn] | tuple[None, None]:
    # Every FFL line starts with 'R'; the actual type is at position 2.
    if len(line) < 2 or line[0] != "R":
        return None, None
    rt = line[1]
    if rt == "F":
        return "FFL.F", parse_ffl_f
    if rt == "T":
        return "FFL.T", parse_ffl_t
    return None, None


def _skip_record(_line: str) -> dict[str, str]:
    """Sentinel parser for records we recognise but defer to a later slice."""
    return {"NOTE": "skipped — record type recognised but not parsed in this slice"}


def _loc_dispatch(line: str) -> tuple[str, ParserFn] | tuple[None, None]:
    # 'RL' = location, 'RG' = location-group (cluster), 'RA' = associated.
    # Only RL is parsed in this slice; RG/RA are recognised and skipped silently
    # (so they don't pollute the rejects list) until the cluster slice lands.
    if len(line) < 2 or line[0] != "R":
        return None, None
    rt = line[1]
    if rt == "L":
        return "LOC.L", parse_loc
    if rt in ("G", "A", "M", "R", "S"):
        # G=location-groups, A=associated, M=memberships, R=route-points,
        # S=synonyms. All recognised; all deferred to later slices.
        return "LOC.SKIP", _skip_record
    return None, None  # genuinely unknown -> quarantined


def _prefix_dispatch(
    kind: str, parser: ParserFn, *, want: str = "R",
) -> Callable[[str], tuple[str | None, ParserFn | None]]:
    """Return a dispatch callable that only accepts lines starting with `want`
    (the universal 'R' file prefix by default). Lines that don't match are
    handed back as (None, None) so `inspect_lines` quarantines them rather
    than feeding garbage into a positional parser."""
    def _dispatch(line: str) -> tuple[str | None, ParserFn | None]:
        if not line.startswith(want):
            return None, None
        return kind, parser
    return _dispatch


def _dis_dispatch(line: str) -> tuple[str | None, ParserFn | None]:
    """DIS has two record types: 'S' (Status, 99 chars) and 'D' (Discount, 18)."""
    if not line:
        return None, None
    rt = line[0]
    if rt == "S":
        return "DIS.S", parse_dis_status
    if rt == "D":
        return "DIS.D", parse_dis_discount
    return None, None


SUFFIX_HANDLERS: dict[str, Callable[[str], tuple[str | None, ParserFn | None]]] = {
    ".FFL": _ffl_dispatch,
    ".FSC": _prefix_dispatch("FSC", parse_fsc, want="R"),
    ".NFO": _prefix_dispatch("NFO", parse_nfo, want="R"),
    ".TTY": _prefix_dispatch("TTY", parse_tty, want="R"),
    ".LOC": _loc_dispatch,
    # RLC/RCM/FRR have no record-type prefix; the whole non-comment line is one
    # record. We use a no-op gate (`want=""`) and rely on the min-length check
    # to quarantine truncated rows.
    ".RLC": _prefix_dispatch("RLC", parse_rlc, want=""),
    ".RCM": _prefix_dispatch("RCM", parse_rcm, want=""),
    ".FRR": _prefix_dispatch("FRR", parse_frr, want=""),
    ".DIS": _dis_dispatch,
}


# Minimum-length check per parsed kind, used by `inspect_lines` to quarantine
# truncated records before the parser slices garbage.
_MIN_LENS: dict[str, int] = {
    "FFL.F": _FFL_F_MIN_LEN,
    "FFL.T": _FFL_T_MIN_LEN,
    "LOC.L": _LOC_MIN_LEN,
    "FSC":   _FSC_RECORD_LEN,
    "NFO":   _NFO_RECORD_LEN,
    "TTY":   _TTY_RECORD_LEN,
    "RLC":   _RLC_RECORD_LEN,
    "RCM":   _RCM_RECORD_LEN,
    "FRR":   _FRR_RECORD_LEN,
    "DIS.S": _DIS_S_RECORD_LEN,
    "DIS.D": _DIS_D_RECORD_LEN,
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
                Reject(i, f"unrecognised record prefix {line[:2]!r}", line)
            )
            continue

        min_len = _MIN_LENS.get(kind)
        if min_len is not None:
            reason = _check_len(line, min_len, kind)
            if reason is not None:
                result.rejects.append(Reject(i, reason, line))
                continue

        if kind.endswith(".SKIP"):
            continue  # recognised-but-deferred record type; don't reject, don't list

        try:
            parsed = parser(line)
        except Exception as exc:
            result.rejects.append(Reject(i, f"parse error in {kind}: {exc!r}", line))
            continue

        parsed["_KIND"] = kind
        parsed["_LINE"] = str(i)
        result.parsed.append(parsed)

    return result


# --- Resolver-facing helpers ----------------------------------------------
# These are used by src.resolver.resolve to drive the lookup. They return
# typed records (with source line numbers) so the resolver can build
# provenance without re-opening the file.


@dataclass(frozen=True)
class FlowRecord:
    """One F-record from a .FFL, with the source line for provenance."""
    line_no: int
    origin_nlc: str
    dest_nlc: str
    route_code: str
    status_code: str
    usage_code: str
    direction: str
    toc: str
    flow_id: str
    raw: dict[str, str]  # the full parsed dict, for provenance detail


@dataclass(frozen=True)
class FareRecord:
    """One T-record from a .FFL, with the source line for provenance."""
    line_no: int
    flow_id: str
    ticket_code: str
    fare_pence: int
    restriction_code: str
    raw: dict[str, str]


# --- mtime-keyed module cache --------------------------------------------
# Loaders below scan multi-MB files; calling them per-query is unusable for
# the impact engine. Cache by (resolved path, mtime_ns, size) so any on-disk
# change invalidates automatically.

_CACHE: dict[tuple[str, int, int, str], object] = {}
_T = TypeVar("_T")


def _cache_key(path: Path, builder: Callable[..., object]) -> tuple[str, int, int, str]:
    """Cache key includes the builder identity so two loaders on the same
    .TTY (e.g. `load_ticket_discount_categories` returning tuples and
    `load_ticket_type_meta` returning TtyRecords) don't collide. Prior to
    this fix the second loader received the first loader's output."""
    st = path.stat()
    return (str(path.resolve()), st.st_mtime_ns, st.st_size, builder.__qualname__)


# Per-key build locks: without them, N concurrent first requests each run the
# multi-minute .FFL parse (cache stampede), starving the API thread pool.
_CACHE_LOCKS: dict[tuple[str, int, int, str], threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cached(path: Path, builder: Callable[[Path], _T]) -> _T:
    key = _cache_key(path, builder)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
        out = builder(path)
        _CACHE[key] = out
        return out


def _iter_ffl_records(feed_path: Path) -> Iterator[tuple[int, str, dict[str, str]]]:
    """Stream parsed records from a .FFL, yielding (line_no, kind, parsed)."""
    with feed_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            kind, parser = _ffl_dispatch(line)
            if parser is None or kind is None:
                continue
            min_len = _MIN_LENS.get(kind)
            if min_len is not None and len(line) < min_len:
                continue
            try:
                yield i, kind, parser(line)
            except Exception:
                continue  # malformed — silent here; caller scans the inspector for rejects.


def find_flows(
    feed_path: Path,
    origin_nlc: str,
    dest_nlc: str,
) -> list[FlowRecord]:
    """Return every F-record matching ORIGIN=origin_nlc AND DEST=dest_nlc.

    Does NOT auto-swap for DIRECTION='R'; the resolver decides whether to
    also query the reverse pair, and the provenance records that choice
    explicitly. Pure read-only scan of the .FFL.
    """
    return find_flows_multi(feed_path, [(origin_nlc, dest_nlc)]).get((origin_nlc, dest_nlc), [])


def find_flows_multi(
    feed_path: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], list[FlowRecord]]:
    """Single-pass FFL scan returning matched F-records grouped by (origin,dest).

    Used by the resolver when cluster fan-out makes us search several
    (origin_group, dest_group) pairs at once — one file scan instead of N.
    Pairs not matched in the file are present as empty lists in the result.
    """
    wanted = set(pairs)
    out: dict[tuple[str, str], list[FlowRecord]] = {p: [] for p in wanted}
    if not wanted:
        return out
    for line_no, kind, rec in _iter_ffl_records(feed_path):
        if kind != "FFL.F":
            continue
        key = (rec["ORIGIN_CODE"], rec["DESTINATION_CODE"])
        if key not in wanted:
            continue
        out[key].append(FlowRecord(
            line_no=line_no,
            origin_nlc=rec["ORIGIN_CODE"],
            dest_nlc=rec["DESTINATION_CODE"],
            route_code=rec["ROUTE_CODE"],
            status_code=rec["STATUS_CODE"],
            usage_code=rec["USAGE_CODE"],
            direction=rec["DIRECTION"],
            toc=rec["TOC"],
            flow_id=rec["FLOW_ID"],
            raw=rec,
        ))
    return out


@dataclass(frozen=True)
class LocationMeta:
    """Subset of a .LOC L-record needed by the resolver for cluster fan-out."""
    nlc: str
    group_nlc: str            # equals `nlc` for group rows themselves
    station_name: str
    crs: str                  # blank for group/fare-only rows
    county: str               # 2-char code; used by the regulation map
    line_no: int


@dataclass(frozen=True)
class FFLIndexes:
    """Indexes built from one .FFL pass. Used by the resolver instead of
    re-scanning the file per query."""
    flows_by_pair: dict[tuple[str, str], list[FlowRecord]]
    fares_by_flow: dict[str, list[FareRecord]]
    # {fare-TOC code -> flows}. Same FlowRecord objects as flows_by_pair
    # (pointers only, ~7MB extra); powers operator-scoped ChangeRequests.
    flows_by_toc: dict[str, list[FlowRecord]]


def load_ffl_indexes(feed_path: Path) -> FFLIndexes:
    """Single-pass FFL build: index of F-records by (origin,dest) and
    T-records by FLOW_ID. Cached on (path, mtime, size); subsequent calls in
    the same process are O(1).

    Deliberately NO on-disk pickle layer: measured on RJFAF805 (9.6M lines),
    unpickling the 1.1GB index (411s) is slower than reparsing (192s) — the
    per-record `raw` dicts dominate both. The warm cost is paid once per
    process; /api/health tells the UI when it's done."""
    return _cached(Path(feed_path), _build_ffl_indexes)


def _build_ffl_indexes(feed_path: Path) -> FFLIndexes:
    flows: dict[tuple[str, str], list[FlowRecord]] = {}
    fares: dict[str, list[FareRecord]] = {}
    by_toc: dict[str, list[FlowRecord]] = {}
    for line_no, kind, rec in _iter_ffl_records(feed_path):
        if kind == "FFL.F":
            key = (rec["ORIGIN_CODE"], rec["DESTINATION_CODE"])
            flows.setdefault(key, []).append(FlowRecord(
                line_no=line_no,
                origin_nlc=rec["ORIGIN_CODE"],
                dest_nlc=rec["DESTINATION_CODE"],
                route_code=rec["ROUTE_CODE"],
                status_code=rec["STATUS_CODE"],
                usage_code=rec["USAGE_CODE"],
                direction=rec["DIRECTION"],
                toc=rec["TOC"],
                flow_id=rec["FLOW_ID"],
                raw=rec,
            ))
            by_toc.setdefault(rec["TOC"], []).append(flows[key][-1])
        elif kind == "FFL.T":
            try:
                pence = int(rec["FARE"])
            except ValueError:
                continue
            fares.setdefault(rec["FLOW_ID"], []).append(FareRecord(
                line_no=line_no,
                flow_id=rec["FLOW_ID"],
                ticket_code=rec["TICKET_CODE"],
                fare_pence=pence,
                restriction_code=rec["RESTRICTION_CODE"].strip(),
                raw=rec,
            ))
    return FFLIndexes(flows_by_pair=flows, fares_by_flow=fares, flows_by_toc=by_toc)


def load_loc_meta(loc_path: Path) -> dict[str, LocationMeta]:
    """Build {NLC -> LocationMeta}. Cached on (path, mtime, size)."""
    return _cached(Path(loc_path), _build_loc_meta)


def _build_loc_meta(loc_path: Path) -> dict[str, LocationMeta]:
    """The .LOC file carries multiple rows per NLC (one per historical date
    range); we keep the row with the latest START_DATE so a current-day query
    gets the current GROUP_NLC. Date sort is string-based on the rearranged
    DDMMYYYY → YYYYMMDD form; correct for the current century, not before."""
    by_nlc: dict[str, LocationMeta] = {}
    by_nlc_start: dict[str, str] = {}
    with loc_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            kind, parser = _loc_dispatch(line)
            if kind != "LOC.L" or parser is None:
                continue
            if len(line) < _LOC_MIN_LEN:
                continue
            try:
                rec = parser(line)
            except Exception:
                continue
            nlc = rec["NLC"]
            start = rec["START_DATE"]
            # Compare as YYYYMMDD-ish: take "DDMMYYYY" -> "YYYYMMDD" for sortability.
            def _key(s: str) -> str:
                return s[4:8] + s[2:4] + s[0:2] if len(s) == 8 else "00000000"
            if nlc in by_nlc and _key(start) <= _key(by_nlc_start[nlc]):
                continue
            by_nlc[nlc] = LocationMeta(
                nlc=nlc,
                group_nlc=rec["GROUP_NLC"],
                station_name=rec["STATION_NAME"].strip(),
                crs=rec["CRS"].strip(),
                county=rec["COUNTY"],
                line_no=i,
            )
            by_nlc_start[nlc] = start
    return by_nlc


@dataclass(frozen=True)
class NfoOverride:
    """One .NFO row that took effect (COMPOSITE='Y' and date-valid).
    `adult_fare_pence` is None when the row is a suppression sentinel."""
    line_no: int
    origin_nlc: str
    dest_nlc: str
    route_code: str            # may be "*****" / blank = any-route wildcard
    railcard_code: str         # 3-char; "   " = no railcard (adult)
    ticket_code: str
    adult_fare_pence: int | None
    is_suppression: bool       # True when ADULT_FARE == "99999999"
    end_date: str              # DDMMYYYY
    start_date: str


# CLAUDE.md sentinel: ADULT_FARE / CHILD_FARE = 99999999 means NO fare
# available (the override is *suppressing* the fare), NOT £999,999.
_NFO_SUPPRESSION_SENTINEL = "99999999"


def load_nfo_overrides(
    nfo_path: Path,
) -> dict[tuple[str, str, str, str, str], list[NfoOverride]]:
    """Build {(origin, dest, route, railcard, ticket) -> [NfoOverride, ...]}.

    Only rows with COMPOSITE_INDICATOR='Y' are loaded (per CLAUDE.md: 'N'
    means ignore — already represented in the flow file). The list-per-key
    shape preserves contradictions (multiple Y rows for the same key) so the
    resolver can surface them instead of picking silently.
    Cached on (path, mtime, size).
    """
    return _cached(Path(nfo_path), _build_nfo_overrides)


def _build_nfo_overrides(
    nfo_path: Path,
) -> dict[tuple[str, str, str, str, str], list[NfoOverride]]:
    out: dict[tuple[str, str, str, str, str], list[NfoOverride]] = {}
    with nfo_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _NFO_RECORD_LEN or line[0] != "R":
                continue
            try:
                rec = parse_nfo(line)
            except Exception:
                continue
            if rec["COMPOSITE_INDICATOR"] != "Y":
                continue  # CLAUDE.md: 'N' = ignore
            adult_raw = rec["ADULT_FARE"]
            is_suppression = adult_raw == _NFO_SUPPRESSION_SENTINEL
            adult_pence: int | None = None
            if not is_suppression:
                try:
                    adult_pence = int(adult_raw)
                except ValueError:
                    continue  # malformed fare; quarantine via inspector if needed
            key = (
                rec["ORIGIN_CODE"],
                rec["DESTINATION_CODE"],
                rec["ROUTE_CODE"],
                rec["RAILCARD_CODE"],
                rec["TICKET_CODE"],
            )
            out.setdefault(key, []).append(NfoOverride(
                line_no=i,
                origin_nlc=key[0],
                dest_nlc=key[1],
                route_code=key[2],
                railcard_code=key[3],
                ticket_code=key[4],
                adult_fare_pence=adult_pence,
                is_suppression=is_suppression,
                end_date=rec["END_DATE"],
                start_date=rec["START_DATE"],
            ))
    return out


def load_fsc_clusters(fsc_path: Path) -> dict[str, list[str]]:
    """Build {MEMBER_NLC -> [CLUSTER_ID, ...]} from the .FSC file. Cached.

    Returns the reverse index: given a station NLC, which cluster IDs include
    it as a member? The resolver uses this for FSC-based blast-radius fan-out
    on top of the LOC GROUP_NLC mapping — they cover different cases
    (LOC groups = big region groups like LON TERMINALS; FSC clusters =
    TOC-specific groupings like LUMO's 'LS57' destination group).
    """
    return _cached(Path(fsc_path), _build_fsc_clusters)


def _build_fsc_clusters(fsc_path: Path) -> dict[str, list[str]]:
    by_member: dict[str, list[str]] = {}
    with fsc_path.open("r", encoding="latin-1") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _FSC_RECORD_LEN or line[0] != "R":
                continue
            try:
                rec = parse_fsc(line)
            except Exception:
                continue
            cluster_id = rec["CLUSTER_ID"]
            member_nlc = rec["MEMBER_NLC"]
            if not cluster_id.strip() or not member_nlc.strip():
                continue
            by_member.setdefault(member_nlc, []).append(cluster_id)
    return by_member


# --- Railcard / status-discount / minimum-fare / rounding loaders ---------
# These build the indexes the resolver walks to apply a railcard discount
# from the feed (RSPS5045 §4.15-4.18) instead of from a hardcoded constant.
# Each loader carries the source line number on every record so the
# resolver's ProvenanceStep can cite the exact feed row.


@dataclass(frozen=True)
class RailcardRecord:
    """One .RLC row (RSPS5045 §4.15.2). `adult_status` is the link into .DIS."""
    line_no: int
    railcard_code: str
    description: str
    adult_status: str          # 3-char status code; "XXX" = not applicable
    child_status: str
    min_passengers: int
    max_passengers: int
    end_date: str
    start_date: str


def load_railcards(rlc_path: Path) -> dict[str, RailcardRecord]:
    """Build {RAILCARD_CODE -> RailcardRecord} from a .RLC. Cached.
    Multiple rows per code may exist (date-versioned); we keep the row with
    the latest START_DATE so a current-day query gets the current statuses."""
    return _cached(Path(rlc_path), _build_railcards)


def _build_railcards(rlc_path: Path) -> dict[str, RailcardRecord]:
    out: dict[str, RailcardRecord] = {}
    latest_start: dict[str, str] = {}
    with rlc_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _RLC_RECORD_LEN:
                continue
            try:
                rec = parse_rlc(line)
            except Exception:
                continue
            code = rec["RAILCARD_CODE"]
            start_key = _ddmmyyyy_sortkey(rec["START_DATE"])
            if code in out and start_key <= latest_start[code]:
                continue
            try:
                min_p = int(rec["MIN_PASSENGERS"])
                max_p = int(rec["MAX_PASSENGERS"])
            except ValueError:
                continue
            out[code] = RailcardRecord(
                line_no=i,
                railcard_code=code,
                description=rec["DESCRIPTION"].strip(),
                adult_status=rec["ADULT_STATUS"],
                child_status=rec["CHILD_STATUS"],
                min_passengers=min_p,
                max_passengers=max_p,
                end_date=rec["END_DATE"],
                start_date=rec["START_DATE"],
            )
            latest_start[code] = start_key
    return out


def _ddmmyyyy_sortkey(s: str) -> str:
    """Rearrange DDMMYYYY into YYYYMMDD for lexical sort. Same convention as
    the .LOC loader. Returns '00000000' for malformed inputs."""
    return s[4:8] + s[2:4] + s[0:2] if len(s) == 8 and s.isdigit() else "00000000"


@dataclass(frozen=True)
class StatusDiscount:
    """One .DIS 'D' row (RSPS5045 §4.17.3). The (status, category) lookup key
    is on the resolver side; this is the value."""
    line_no: int
    status_code: str
    discount_category: str
    discount_indicator: str   # '0'/'F'/'M'/'H'/'L'/'X'/'N'
    discount_percentage: int  # tenths of a percent — 334 means 33.4%
    end_date: str


def load_status_discounts(dis_path: Path) -> dict[tuple[str, str], StatusDiscount]:
    """Build {(STATUS_CODE, DISCOUNT_CATEGORY) -> StatusDiscount}. Cached.
    Only 'D' records are indexed; 'S' (status) records have flat-fare caps
    consumed by the 'F'/'M'/'H'/'L' indicators — wired in when those land."""
    return _cached(Path(dis_path), _build_status_discounts)


def _build_status_discounts(dis_path: Path) -> dict[tuple[str, str], StatusDiscount]:
    out: dict[tuple[str, str], StatusDiscount] = {}
    with dis_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if not line.startswith("D") or len(line) < _DIS_D_RECORD_LEN:
                continue
            try:
                rec = parse_dis_discount(line)
                pct = int(rec["DISCOUNT_PERCENTAGE"])
            except (ValueError, KeyError):
                continue
            key = (rec["STATUS_CODE"], rec["DISCOUNT_CATEGORY"])
            # If a key recurs we keep the last (highest line number), which in
            # a date-ordered feed is the most recent. Date-rank disambiguation
            # is overkill for the current slice; revisit when the resolver
            # starts honouring END_DATE.
            out[key] = StatusDiscount(
                line_no=i,
                status_code=rec["STATUS_CODE"],
                discount_category=rec["DISCOUNT_CATEGORY"],
                discount_indicator=rec["DISCOUNT_INDICATOR"],
                discount_percentage=pct,
                end_date=rec["END_DATE"],
            )
    return out


@dataclass(frozen=True)
class RcmMinFare:
    """One .RCM row (RSPS5045 §4.16.2). Adult fares only."""
    line_no: int
    railcard_code: str
    ticket_code: str
    minimum_fare_pence: int
    end_date: str
    start_date: str


def load_rcm_min_fares(rcm_path: Path) -> dict[tuple[str, str], RcmMinFare]:
    """Build {(RAILCARD_CODE, TICKET_CODE) -> RcmMinFare}. Cached.
    Keeps the row with the latest START_DATE per key."""
    return _cached(Path(rcm_path), _build_rcm_min_fares)


def _build_rcm_min_fares(rcm_path: Path) -> dict[tuple[str, str], RcmMinFare]:
    out: dict[tuple[str, str], RcmMinFare] = {}
    latest_start: dict[tuple[str, str], str] = {}
    with rcm_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _RCM_RECORD_LEN:
                continue
            try:
                rec = parse_rcm(line)
                pence = int(rec["MINIMUM_FARE"])
            except (ValueError, KeyError):
                continue
            key = (rec["RAILCARD_CODE"], rec["TICKET_CODE"])
            start_key = _ddmmyyyy_sortkey(rec["START_DATE"])
            if key in out and start_key <= latest_start[key]:
                continue
            out[key] = RcmMinFare(
                line_no=i,
                railcard_code=key[0],
                ticket_code=key[1],
                minimum_fare_pence=pence,
                end_date=rec["END_DATE"],
                start_date=rec["START_DATE"],
            )
            latest_start[key] = start_key
    return out


@dataclass(frozen=True)
class FrrBand:
    """One .FRR row (RSPS5045 §4.18.2). A rounding rule is a series of bands
    in ascending RULE_INDEX order; the first band whose MAX_AMOUNT >= the
    fare wins, and the fare is rounded UP to that ROUND_AMOUNT."""
    line_no: int
    rule_no: str
    rule_index: str
    max_amount_pence: int
    round_amount_pence: int


def load_frr_rules(frr_path: Path) -> dict[str, list[FrrBand]]:
    """Build {RULE_NO -> [FrrBand sorted by RULE_INDEX]} from a .FRR. Cached."""
    return _cached(Path(frr_path), _build_frr_rules)


def _build_frr_rules(frr_path: Path) -> dict[str, list[FrrBand]]:
    by_rule: dict[str, list[FrrBand]] = {}
    with frr_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _FRR_RECORD_LEN:
                continue
            try:
                rec = parse_frr(line)
                max_p = int(rec["MAX_AMOUNT"])
                round_p = int(rec["ROUND_AMOUNT"])
            except (ValueError, KeyError):
                continue
            band = FrrBand(
                line_no=i,
                rule_no=rec["RULE_NO"],
                rule_index=rec["RULE_INDEX"],
                max_amount_pence=max_p,
                round_amount_pence=round_p,
            )
            by_rule.setdefault(rec["RULE_NO"], []).append(band)
    for bands in by_rule.values():
        bands.sort(key=lambda b: b.rule_index)
    return by_rule


def load_ticket_discount_categories(tty_path: Path) -> dict[str, tuple[int, str]]:
    """Build {TICKET_CODE -> (line_no, DISCOUNT_CATEGORY)} from a .TTY. Cached.
    Only the link into the status-discount table is exposed; full TTY metadata
    is available via parse_tty() when callers need it. Keeps the row with the
    latest START_DATE per ticket code."""
    return _cached(Path(tty_path), _build_ticket_discount_categories)


def _build_ticket_discount_categories(tty_path: Path) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    latest_start: dict[str, str] = {}
    with tty_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _TTY_RECORD_LEN or not line.startswith("R"):
                continue
            try:
                rec = parse_tty(line)
            except Exception:
                continue
            code = rec["TICKET_CODE"]
            start_key = _ddmmyyyy_sortkey(rec["START_DATE"])
            if code in out and start_key <= latest_start[code]:
                continue
            out[code] = (i, rec["DISCOUNT_CATEGORY"])
            latest_start[code] = start_key
    return out


@dataclass(frozen=True)
class TtyRecord:
    """Fuller .TTY metadata used by the regulation map / impact engine.

    `load_ticket_discount_categories` (above) exposes only the
    (line_no, DISCOUNT_CATEGORY) pair the resolver's railcard chain needs.
    The regulation classifier needs more (description for ADVANCE-detection;
    TKT_CLASS/TKT_GROUP/TKT_TYPE for the §1 walk-up rules) so we expose those
    here as a sibling loader without touching the resolver's index shape."""
    line_no: int
    ticket_code: str
    description: str           # uppercase, 15-char; e.g. "OFF-PEAK R", "ADVANCE", "ANYTIME R"
    tkt_class: str             # '1'=First, '2'=Standard, '9'=other
    tkt_type: str              # 'S'=single, 'R'=return, 'N'=season
    tkt_group: str             # 'F'=first, 'S'=standard, 'P'=promo, 'E'=euro
    discount_category: str     # 2-char; links to .DIS
    end_date: str
    start_date: str


def load_ticket_type_meta(tty_path: Path) -> dict[str, TtyRecord]:
    """Build {TICKET_CODE -> TtyRecord} from a .TTY. Cached on (path, mtime, size).

    Keeps the row with the latest START_DATE per ticket code (same convention
    as load_ticket_discount_categories). Independent of that loader: callers
    that only want DISCOUNT_CATEGORY for the resolver railcard chain keep
    using the lightweight loader; callers that need full metadata use this."""
    return _cached(Path(tty_path), _build_ticket_type_meta)


def _build_ticket_type_meta(tty_path: Path) -> dict[str, TtyRecord]:
    out: dict[str, TtyRecord] = {}
    latest_start: dict[str, str] = {}
    with tty_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            if len(line) < _TTY_RECORD_LEN or not line.startswith("R"):
                continue
            try:
                rec = parse_tty(line)
            except Exception:
                continue
            code = rec["TICKET_CODE"]
            start_key = _ddmmyyyy_sortkey(rec["START_DATE"])
            if code in out and start_key <= latest_start[code]:
                continue
            out[code] = TtyRecord(
                line_no=i,
                ticket_code=code,
                description=rec["DESCRIPTION"].strip().upper(),
                tkt_class=rec["TKT_CLASS"],
                tkt_type=rec["TKT_TYPE"],
                tkt_group=rec["TKT_GROUP"],
                discount_category=rec["DISCOUNT_CATEGORY"],
                end_date=rec["END_DATE"],
                start_date=rec["START_DATE"],
            )
            latest_start[code] = start_key
    return out


@dataclass(frozen=True)
class TocRecord:
    """One F-record from a .TOC: fare-TOC code -> operator name.

    The .FFL carries 3-char fare-TOC codes (e.g. 'NTH', 'GWR'); the 2-char id
    is the timetable TOC (e.g. 'NT') used elsewhere (corridors.json)."""
    line_no: int
    fare_toc: str      # 3-char, joins to FlowRecord.toc
    toc_2char: str     # 2-char timetable id; may be blank
    name: str


def load_toc_meta(toc_path: Path) -> dict[str, TocRecord]:
    """Build {fare-TOC code -> TocRecord} from a .TOC. Cached on (path, mtime, size)."""
    return _cached(Path(toc_path), _build_toc_meta)


def _build_toc_meta(toc_path: Path) -> dict[str, TocRecord]:
    out: dict[str, TocRecord] = {}
    with toc_path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            # .TOC rows have no 'R' prefix: RECORD_TYPE at pos 1 ('F'=TOC,
            # 'T'=fare-TOC map row). Only F rows carry the operator name.
            if _slice(line, 1, 1) != "F" or len(line) < 7:
                continue
            code = _slice(line, 2, 3).strip()
            if not code:
                continue
            out[code] = TocRecord(
                line_no=i,
                fare_toc=code,
                toc_2char=_slice(line, 5, 2).strip(),
                name=_slice(line, 7, 30).strip(),
            )
    return out


# --- Raw feed lines for provenance -----------------------------------------
# A provenance step cites `line N of <file>`; the SOURCE RECORD inspector
# wants the original fixed-width line. linecache would keep the whole file's
# lines resident (~900MB for the 253MB .FFL) so we use a sparse offset table:
# the byte offset of every 10,000th line (~8KB for 9.6M lines), then seek and
# scan at most 10,000 lines (~500KB) per lookup.

_OFFSET_STRIDE = 10_000


@dataclass(frozen=True)
class _LineOffsets:
    stride: int
    offsets: tuple[int, ...]  # offsets[k] = byte offset of line k*stride + 1


def _build_line_offsets(path: Path) -> _LineOffsets:
    offsets = [0]
    line = 0
    pos = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(4 << 20)
            if not chunk:
                break
            idx = 0
            while True:
                nl = chunk.find(b"\n", idx)
                if nl == -1:
                    break
                line += 1
                if line % _OFFSET_STRIDE == 0:
                    offsets.append(pos + nl + 1)
                idx = nl + 1
            pos += len(chunk)
    return _LineOffsets(stride=_OFFSET_STRIDE, offsets=tuple(offsets))


def raw_feed_line(path: Path, line_no: int) -> str | None:
    """Return raw line `line_no` (1-based, newline stripped) of a feed file,
    or None if out of range. O(1) memory; offset table cached per file."""
    if line_no < 1:
        return None
    try:
        table: _LineOffsets = _cached(Path(path), _build_line_offsets)
    except OSError:
        return None
    k = (line_no - 1) // table.stride
    if k >= len(table.offsets):
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(table.offsets[k])
            for _ in range(line_no - 1 - k * table.stride):
                if not fh.readline():
                    return None
            raw = fh.readline()
    except OSError:
        return None
    if not raw:
        return None
    return raw.rstrip(b"\r\n").decode("latin-1")


def find_fares(feed_path: Path, flow_id: str) -> list[FareRecord]:
    """Return every T-record under the given FLOW_ID."""
    out: list[FareRecord] = []
    for line_no, kind, rec in _iter_ffl_records(feed_path):
        if kind != "FFL.T":
            continue
        if rec["FLOW_ID"] != flow_id:
            continue
        try:
            pence = int(rec["FARE"])
        except ValueError:
            continue  # malformed fare — quarantine via the inspector if needed.
        out.append(FareRecord(
            line_no=line_no,
            flow_id=rec["FLOW_ID"],
            ticket_code=rec["TICKET_CODE"],
            fare_pence=pence,
            restriction_code=rec["RESTRICTION_CODE"].strip(),
            raw=rec,
        ))
    return out


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
