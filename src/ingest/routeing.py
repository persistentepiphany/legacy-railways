"""RSPS5047 National Routeing Guide feed parser.

The routeing feed is CSV, not fixed-width like the fares feed (RSPS5045).
Every line is either a comment (leading '/') or a record; empty fields
appear as adjacent commas with no space padding (spec §5.3).  Files are
bracketed by a `/!! Start of file` header block and a
`/!! End of file (N records) (dd/mm/yyyy)` terminator (spec §5.4, §5.7).

Files parsed here (spec §4.2 table, RJRGnnnn.EXT naming):

    .RGS  Station               § 6.2   station -> up to 4 routeing points + group
    .RGG  Station Group         § 6.3   group id -> main station CRS
    .RGP  Routeing Points list  § 6.4   canonical list of routeing points
    .RGN  Nodes                 § 6.5   routeing points + interchanges
    .RGM  Maps                  § 6.6   map codes
    .RGL  Links                 § 6.7   directional link between two nodes on a map
    .RGR  Permitted Routes      § 6.8   (start_rp, end_rp) -> map sequence
    .RGD  Station-Link          § 6.9   distances between adjacent stations
    .RGF  Easement Definition   § 6.10  four record types (E / L / D / X)
    .RGH  Easement TOC          § 6.11  which TOC published the text
    .RGC  London Stations       § 6.13  Cross-London eligible stations
    .RGY  Locations             § 6.15  CRS <-> NLC cross-reference
    .RGE  Easement Text         § 6.16  free-form English up to 2000 chars

Encoding: ASCII per § 5.2, but we open latin-1 to match the rest of the
project's RDG parsers (tolerant of stray non-ASCII bytes in DTD exports).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from src.ingest.inspect import _cached


# --- Record dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class StationRecord:
    """RSPS5047 § 6.2.2 — .RGS row."""
    station_crs: str
    routeing_points: tuple[str, ...]  # 0-4 entries; empty when station IS a routeing point
    station_group_id: str | None      # 'Gnn' or None


@dataclass(frozen=True)
class StationGroup:
    """RSPS5047 § 6.3.2 — .RGG row."""
    group_id: str      # e.g. 'G02'
    main_station: str  # CRS of the preferred station


@dataclass(frozen=True)
class LinkRecord:
    """RSPS5047 § 6.7.2 — .RGL row. Directional; reverse is its own record."""
    start_node: str
    end_node: str
    map_code: str


@dataclass(frozen=True)
class PermittedRoute:
    """RSPS5047 § 6.8.2 — .RGR row.

    `map_sequence` is the ordered list of map codes making a continuous
    geographical path from start_routeing_point to end_routeing_point.
    A single-element `('LO',)` means the route is via London (§ 6.8.1.3).
    Each permitted route has a matching reverse row in the feed."""
    start_routeing_point: str
    end_routeing_point: str
    map_sequence: tuple[str, ...]


@dataclass(frozen=True)
class StationLinkDistance:
    """RSPS5047 § 6.9 — .RGD row.  Physical distance between adjacent stations
    (miles/chains per the spec; kept as raw strings until a caller needs
    them numerically)."""
    from_crs: str
    to_crs: str
    distance: str


# --- Easement records (.RGF) -----------------------------------------------

EasementType = Literal["1", "2", "3", "4"]      # sleeper / disabled / normal / service variation
EasementClass = Literal["1", "2"]                # 1 = positive, 2 = negative
EasementCategory = Literal["1", "2", "3", "4", "5", "6", "7"]
LocationModifier = Literal["1", "2", "3", "4", "5", "6"]  # applicable/origin/dest/via/exclude/doubleback


@dataclass(frozen=True)
class EasementHeader:
    """RSPS5047 § 6.10.2 — E-record.

    `easement_class == '2'` = a NEGATIVE easement (removes an otherwise
    permitted route/journey).  A journey with a matching negative easement
    is denied; a matching positive easement grants permission the base
    permitted-routes table would not.
    """
    easement_ref: str
    start_date: str      # ddmmyyyy or '' if null
    end_date: str        # '31122999' = until further notice
    text_ref: str
    easement_type: EasementType
    easement_class: EasementClass
    category: EasementCategory
    valid_days: str      # 7 chars, 'Y'/'N' Mon..Sun; may be '' for always
    start_time: str      # hhmm or ''
    end_time: str        # hhmm or ''


@dataclass(frozen=True)
class EasementLocation:
    """§ 6.10.3 — L-record. Says which location(s) the easement is scoped to."""
    easement_ref: str
    location_crs: str
    modifier: LocationModifier  # 1=applicable 2=origin 3=dest 4=via 5=exclude 6=doubleback


@dataclass(frozen=True)
class EasementDetail:
    """§ 6.10.4 — D-record. Says which TOC/ticket/route/UID the easement applies to."""
    easement_ref: str
    detail_type: Literal["1", "2", "3", "4"]  # 1=UID 2=TOC 3=route 4=ticket
    detail_code: str


@dataclass(frozen=True)
class EasementException:
    """§ 6.10.5 — X-record. Says which TOC/UID the easement does NOT apply to."""
    easement_ref: str
    exception_type: Literal["1", "2"]  # 1=UID 2=TOC
    exception_code: str


@dataclass(frozen=True)
class EasementBundle:
    """All records for a single EASEMENT_REF, joined.  Populated by
    `load_easements`.  Callers work off this bundle, not the raw records."""
    header: EasementHeader
    locations: tuple[EasementLocation, ...]
    details: tuple[EasementDetail, ...]
    exceptions: tuple[EasementException, ...]


@dataclass(frozen=True)
class EasementText:
    """§ 6.16.2 — .RGE row.  Free-form English up to 2000 chars."""
    text_ref: str
    text: str


@dataclass(frozen=True)
class EasementTocRow:
    """§ 6.11.2 — .RGH row.  Which TOC published the easement text."""
    text_ref: str
    toc: str


@dataclass(frozen=True)
class LocationCrossRef:
    """§ 6.15 — .RGY row.  CRS ↔ NLC cross-reference."""
    crs: str
    nlc: str


# --- Common utilities ------------------------------------------------------


def _iter_records(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line_no, line) for every non-comment, non-empty line.

    Comments (§ 5.3) start with '/'.  The `/!! Start of file` and `/!! End
    of file` markers are comments and dropped here.  Line numbers are
    1-indexed and preserved so callers can build provenance
    ("data/RJRG0042.RGF line 137")."""
    with path.open("r", encoding="latin-1") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("/"):
                continue
            yield i, line


def _fields(line: str, expected: int | None = None) -> list[str]:
    """Split a CSV line, honouring the "empty = adjacent commas" rule.

    Does NOT trim whitespace: § 5.3 doesn't allow padding, so any spaces we
    see are semantic and should be preserved (e.g. a TOC field padded to 2
    chars with a trailing space per § 6.11.2)."""
    parts = line.split(",")
    if expected is not None and len(parts) < expected:
        parts.extend([""] * (expected - len(parts)))
    return parts


# --- .RGS  Station ---------------------------------------------------------


def _build_stations(path: Path) -> dict[str, StationRecord]:
    out: dict[str, StationRecord] = {}
    for _, line in _iter_records(path):
        f = _fields(line, expected=6)
        crs = f[0]
        rps = tuple(x for x in (f[1], f[2], f[3], f[4]) if x)
        group = f[5] or None
        out[crs] = StationRecord(station_crs=crs, routeing_points=rps, station_group_id=group)
    return out


def load_stations(rgs_path: Path) -> dict[str, StationRecord]:
    """Return CRS -> StationRecord."""
    return _cached(Path(rgs_path), _build_stations)


# --- .RGG  Station Group ---------------------------------------------------


def _build_station_groups(path: Path) -> dict[str, StationGroup]:
    out: dict[str, StationGroup] = {}
    for _, line in _iter_records(path):
        f = _fields(line, expected=2)
        out[f[0]] = StationGroup(group_id=f[0], main_station=f[1])
    return out


def load_station_groups(rgg_path: Path) -> dict[str, StationGroup]:
    return _cached(Path(rgg_path), _build_station_groups)


# --- .RGP  Routeing Points -------------------------------------------------


def _build_routeing_points(path: Path) -> frozenset[str]:
    return frozenset(line.split(",", 1)[0] for _, line in _iter_records(path))


def load_routeing_points(rgp_path: Path) -> frozenset[str]:
    """The canonical set of routeing-point identifiers (CRS or group id)."""
    return _cached(Path(rgp_path), _build_routeing_points)


# --- .RGN  Nodes  ----------------------------------------------------------


def _build_nodes(path: Path) -> frozenset[str]:
    return frozenset(line.split(",", 1)[0] for _, line in _iter_records(path))


def load_nodes(rgn_path: Path) -> frozenset[str]:
    """Routeing points + interchange points (superset of .RGP)."""
    return _cached(Path(rgn_path), _build_nodes)


# --- .RGL  Links (per-map graph edges) -------------------------------------


def _build_links(path: Path) -> tuple[LinkRecord, ...]:
    out: list[LinkRecord] = []
    for _, line in _iter_records(path):
        f = _fields(line, expected=3)
        out.append(LinkRecord(start_node=f[0], end_node=f[1], map_code=f[2]))
    return tuple(out)


def load_links(rgl_path: Path) -> tuple[LinkRecord, ...]:
    return _cached(Path(rgl_path), _build_links)


# --- .RGR  Permitted Routes ------------------------------------------------


def _build_permitted_routes(path: Path) -> dict[tuple[str, str], tuple[PermittedRoute, ...]]:
    """Keyed by (start_rp, end_rp).  Multiple routes per pair are common —
    each is a distinct map sequence (§ 6.8.1.2)."""
    grouped: dict[tuple[str, str], list[PermittedRoute]] = {}
    for _, line in _iter_records(path):
        f = _fields(line, expected=3)
        start, end = f[0], f[1]
        # Remaining fields are the map sequence: § 6.8.2 length is 2*n
        # ("one or more map codes ... comma-separated").
        maps = tuple(m for m in f[2:] if m)
        pr = PermittedRoute(start_routeing_point=start, end_routeing_point=end, map_sequence=maps)
        grouped.setdefault((start, end), []).append(pr)
    return {k: tuple(v) for k, v in grouped.items()}


def load_permitted_routes(
    rgr_path: Path,
) -> dict[tuple[str, str], tuple[PermittedRoute, ...]]:
    return _cached(Path(rgr_path), _build_permitted_routes)


# --- .RGD  Station-Link distances ------------------------------------------


def _build_station_link_distances(path: Path) -> tuple[StationLinkDistance, ...]:
    out: list[StationLinkDistance] = []
    for _, line in _iter_records(path):
        f = _fields(line, expected=3)
        out.append(StationLinkDistance(from_crs=f[0], to_crs=f[1], distance=f[2]))
    return tuple(out)


def load_station_link_distances(rgd_path: Path) -> tuple[StationLinkDistance, ...]:
    return _cached(Path(rgd_path), _build_station_link_distances)


# --- .RGF  Easement Definition (E / L / D / X records) --------------------


def _build_easements(path: Path) -> dict[str, EasementBundle]:
    """Merge the four record types into one bundle per EASEMENT_REF.

    Order in the file is E, then that easement's L/D/X (§ 5.6 says records
    are ASCII-sorted, and the RECORD_TYPE column is field 1, so E-records
    for a ref come before L/D/X for the same ref — but we don't rely on
    order; we group by ref.)"""
    headers: dict[str, EasementHeader] = {}
    locs: dict[str, list[EasementLocation]] = {}
    dets: dict[str, list[EasementDetail]] = {}
    exs: dict[str, list[EasementException]] = {}

    for _, line in _iter_records(path):
        f = _fields(line)
        rt = f[0] if f else ""
        if rt == "E":
            # § 6.10.2 layout (11 fields).
            f = _fields(line, expected=11)
            hdr = EasementHeader(
                easement_ref=f[1],
                start_date=f[2],
                end_date=f[3],
                text_ref=f[4],
                easement_type=f[5],       # type: ignore[arg-type]
                easement_class=f[6],      # type: ignore[arg-type]
                category=f[7],            # type: ignore[arg-type]
                valid_days=f[8],
                start_time=f[9],
                end_time=f[10],
            )
            headers[hdr.easement_ref] = hdr
        elif rt == "L":
            f = _fields(line, expected=4)
            loc = EasementLocation(
                easement_ref=f[1],
                location_crs=f[2],
                modifier=f[3],            # type: ignore[arg-type]
            )
            locs.setdefault(loc.easement_ref, []).append(loc)
        elif rt == "D":
            f = _fields(line, expected=4)
            det = EasementDetail(
                easement_ref=f[1],
                detail_type=f[2],         # type: ignore[arg-type]
                detail_code=f[3],
            )
            dets.setdefault(det.easement_ref, []).append(det)
        elif rt == "X":
            f = _fields(line, expected=4)
            exc = EasementException(
                easement_ref=f[1],
                exception_type=f[2],      # type: ignore[arg-type]
                exception_code=f[3],
            )
            exs.setdefault(exc.easement_ref, []).append(exc)
        # Unknown record types (future spec expansion) are dropped rather
        # than crashing — the resolver's "never silently guess" rule
        # applies to fares, not to defensive parser guards.

    bundles: dict[str, EasementBundle] = {}
    for ref, hdr in headers.items():
        bundles[ref] = EasementBundle(
            header=hdr,
            locations=tuple(locs.get(ref, ())),
            details=tuple(dets.get(ref, ())),
            exceptions=tuple(exs.get(ref, ())),
        )
    return bundles


def load_easements(rgf_path: Path) -> dict[str, EasementBundle]:
    """Return EASEMENT_REF -> EasementBundle."""
    return _cached(Path(rgf_path), _build_easements)


# --- .RGE  Easement Text ---------------------------------------------------


def _build_easement_texts(path: Path) -> dict[str, EasementText]:
    """§ 6.16.2: "Any commas embedded in this text are part of the text; they
    are not to be considered record separators."  So we split on the FIRST
    comma only — TEXT_REF then the free-form body up to 2000 chars."""
    out: dict[str, EasementText] = {}
    for _, line in _iter_records(path):
        head, _, body = line.partition(",")
        out[head] = EasementText(text_ref=head, text=body)
    return out


def load_easement_texts(rge_path: Path) -> dict[str, EasementText]:
    """Return TEXT_REF -> EasementText."""
    return _cached(Path(rge_path), _build_easement_texts)


# --- .RGH  Easement TOC ----------------------------------------------------


def _build_easement_tocs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for _, line in _iter_records(path):
        f = _fields(line, expected=2)
        out[f[0]] = f[1].strip() or ""  # spec § 6.11.2 permits spaces if unrecorded
    return out


def load_easement_tocs(rgh_path: Path) -> dict[str, str]:
    """Return TEXT_REF -> TOC code ('' if unrecorded)."""
    return _cached(Path(rgh_path), _build_easement_tocs)


# --- .RGC  London Stations (Cross-London) ----------------------------------


def _build_london_stations(path: Path) -> frozenset[str]:
    return frozenset(line.split(",", 1)[0] for _, line in _iter_records(path))


def load_london_stations(rgc_path: Path) -> frozenset[str]:
    """Stations eligible for Cross-London processing.  Paired with map code
    `LO` in the permitted routes table (§ 6.6.1.1)."""
    return _cached(Path(rgc_path), _build_london_stations)


# --- .RGY  Locations CRS<->NLC --------------------------------------------


def _build_locations_xref(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (crs_to_nlc, nlc_to_crs).  The RSPS5047 spec § 6.15 is thin on
    the exact column order — we defensively take the first two non-empty
    fields.  The map is a fallback anyway: the primary CRS↔NLC source is
    the fares feed .LOC via `src.ingest.inspect.load_loc_meta`."""
    crs_to_nlc: dict[str, str] = {}
    nlc_to_crs: dict[str, str] = {}
    for _, line in _iter_records(path):
        f = [x for x in _fields(line) if x]
        if len(f) < 2:
            continue
        a, b = f[0], f[1]
        # CRS is 3 alpha chars, NLC is 4+ digits — use that to orient.
        if a.isalpha() and len(a) == 3 and b.isdigit():
            crs_to_nlc[a] = b
            nlc_to_crs[b] = a
        elif b.isalpha() and len(b) == 3 and a.isdigit():
            crs_to_nlc[b] = a
            nlc_to_crs[a] = b
    return crs_to_nlc, nlc_to_crs


def load_locations_xref(rgy_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    return _cached(Path(rgy_path), _build_locations_xref)


__all__ = [
    "EasementBundle",
    "EasementCategory",
    "EasementClass",
    "EasementDetail",
    "EasementException",
    "EasementHeader",
    "EasementLocation",
    "EasementText",
    "EasementTocRow",
    "EasementType",
    "LinkRecord",
    "LocationCrossRef",
    "LocationModifier",
    "PermittedRoute",
    "StationGroup",
    "StationLinkDistance",
    "StationRecord",
    "load_easement_texts",
    "load_easement_tocs",
    "load_easements",
    "load_links",
    "load_locations_xref",
    "load_london_stations",
    "load_nodes",
    "load_permitted_routes",
    "load_routeing_points",
    "load_station_groups",
    "load_station_link_distances",
    "load_stations",
]
