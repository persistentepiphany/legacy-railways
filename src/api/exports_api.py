"""FastAPI router for download / upload / route-display endpoints.

Every route here is deterministic and side-effect-free with respect to the
baseline fare graph and staging layer. Downloads emit exactly what the
corresponding JSON endpoints show — a CSV row and a UI row for the same
corridor cannot disagree. Uploads only parse in-memory: nothing is written
to disk and nothing enters the impact engine or resolver from an upload.

Endpoints (all under /api):
  GET  /api/export/corridors.{csv,xlsx}    → curated corridor summary
  GET  /api/export/staging.{csv,xlsx}      → pending + approved queue
  GET  /api/export/fares.{csv,xlsx}?...    → baseline fares for one corridor
  GET  /api/export/feed.{csv,xlsx}?...     → a slice of an on-disk feed file
  POST /api/inspect/parse                  → parsed records + rejects (JSON)
  POST /api/inspect/parse.csv              → CSV; two sections stitched
  POST /api/inspect/parse.xlsx             → XLSX with 'records' + 'rejects'
  GET  /api/route/svg?...                  → inline SVG route diagram
  GET  /api/inspect/suffixes               → supported file suffixes
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from src.api.exports import (
    corridor_report,
    default_filename,
    fares_report,
    parsed_records_report,
    rows_to_csv,
    staging_report,
    write_xlsx_bytes,
)
from src.impact.baseline_scan import baseline_affected
from src.impact.feed_paths import FeedPaths
from src.ingest.inspect import (
    Reject,
    SUFFIX_HANDLERS,
    inspect_lines,
    load_loc_meta,
)


router = APIRouter()


# --- Small helpers ---------------------------------------------------------


def _csv_response(kind: str, columns: Sequence[str], rows: list[dict]) -> Response:
    body = rows_to_csv(list(columns), rows).encode("utf-8")
    fname = default_filename(kind, "csv")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _xlsx_response(kind: str, sheets: Sequence[tuple[str, Sequence[str], list[dict]]]) -> Response:
    body = write_xlsx_bytes(sheets)
    fname = default_filename(kind, "xlsx")
    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _svg_response(body: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type="image/svg+xml; charset=utf-8",
    )


# --- Curated-corridor summary export --------------------------------------


def _overview_cache_or_400(request: Request) -> dict:
    cache = request.app.state.overview
    if cache is None:
        raise ValueError(
            "overview baseline still computing on the warm thread — "
            "poll /api/overview until ready before requesting a download"
        )
    return cache


@router.get("/api/export/corridors.csv")
def export_corridors_csv(request: Request) -> Response:
    cache = _overview_cache_or_400(request)
    columns, rows = corridor_report(cache)
    return _csv_response("corridors", columns, rows)


@router.get("/api/export/corridors.xlsx")
def export_corridors_xlsx(request: Request) -> Response:
    cache = _overview_cache_or_400(request)
    columns, rows = corridor_report(cache)
    return _xlsx_response("corridors", [("corridors", columns, rows)])


# --- Staging queue export --------------------------------------------------


@router.get("/api/export/staging.csv")
def export_staging_csv(request: Request) -> Response:
    columns, rows = staging_report(request.app.state.staging)
    return _csv_response("staging", columns, rows)


@router.get("/api/export/staging.xlsx")
def export_staging_xlsx(request: Request) -> Response:
    columns, rows = staging_report(request.app.state.staging)
    return _xlsx_response("staging", [("staging", columns, rows)])


# --- Baseline fares for one corridor --------------------------------------


def _looks_like_nlc(s: str) -> bool:
    return len(s) == 4 and all(ch.isalnum() for ch in s)


@router.get("/api/export/fares.csv")
def export_fares_csv(
    request: Request,
    origin_nlc: str = Query(..., min_length=4, max_length=4),
    dest_nlc: str = Query(..., min_length=4, max_length=4),
) -> Response:
    return _fares_response(request, origin_nlc, dest_nlc, fmt="csv")


@router.get("/api/export/fares.xlsx")
def export_fares_xlsx(
    request: Request,
    origin_nlc: str = Query(..., min_length=4, max_length=4),
    dest_nlc: str = Query(..., min_length=4, max_length=4),
) -> Response:
    return _fares_response(request, origin_nlc, dest_nlc, fmt="xlsx")


def _fares_response(request: Request, origin_nlc: str, dest_nlc: str, *, fmt: str) -> Response:
    if not (_looks_like_nlc(origin_nlc) and _looks_like_nlc(dest_nlc)):
        raise ValueError("origin_nlc and dest_nlc must be 4-char NLC codes")
    fp: FeedPaths = request.app.state.feed_paths
    loc = load_loc_meta(fp.loc) if fp.loc.exists() else {}
    origin_meta = loc.get(origin_nlc)
    dest_meta = loc.get(dest_nlc)
    origin_name = (origin_meta.station_name.strip() if origin_meta else "")
    dest_name = (dest_meta.station_name.strip() if dest_meta else "")
    affected = baseline_affected(origin_nlc, dest_nlc, fp)
    columns, rows = fares_report(affected, origin_name, dest_name)
    kind = f"fares-{origin_nlc}-{dest_nlc}"
    if fmt == "csv":
        return _csv_response(kind, columns, rows)
    return _xlsx_response(kind, [(f"{origin_nlc}-{dest_nlc}", columns, rows)])


# --- On-disk feed slice export --------------------------------------------


_SUFFIX_TO_FEED_PATH = {
    ".FFL": "ffl", ".LOC": "loc", ".FSC": "fsc", ".NFO": "nfo",
    ".RLC": "rlc", ".DIS": "dis", ".RCM": "rcm", ".FRR": "frr", ".TTY": "tty",
}


def _resolve_feed_path(fp: FeedPaths, suffix: str) -> Path:
    key = _SUFFIX_TO_FEED_PATH.get(suffix.upper())
    if key is None:
        raise ValueError(
            f"unknown feed suffix {suffix!r}; supported: "
            f"{sorted(_SUFFIX_TO_FEED_PATH)}"
        )
    path = getattr(fp, key)
    if not path or not path.exists():
        raise FileNotFoundError(str(path))
    return path


def _parse_feed_slice(path: Path, suffix: str, limit: int) -> tuple[list[dict], list[dict]]:
    """Parse the first `limit` non-comment records of an on-disk feed file
    into (parsed_rows, reject_dicts). Streams the file so a 300MB .FFL
    doesn't blow up memory just to get 500 sample records."""
    with path.open("r", encoding="latin-1") as fh:
        def gen():
            for line in fh:
                yield line
        result = inspect_lines(_bounded(gen(), limit), suffix.upper())
    rejects = [{"line_no": r.line_no, "reason": r.reason, "raw": r.raw}
               for r in result.rejects]
    return result.parsed, rejects


def _bounded(lines, limit: int):
    """Yield up to `limit` non-comment lines from `lines`, preserving line
    number semantics by streaming everything through inspect_lines.
    (Comments still count against the limit so the caller sees a stable
    slice head — the whole point of the export is a peek, not a full parse.)"""
    for i, line in enumerate(lines, start=1):
        if i > limit:
            return
        yield line


@router.get("/api/inspect/suffixes")
def api_inspect_suffixes() -> dict:
    """Which RDG feed suffixes the parser knows about."""
    return {"supported": sorted(SUFFIX_HANDLERS.keys())}


@router.get("/api/export/feed.csv")
def export_feed_csv(
    request: Request,
    suffix: str = Query(..., description="Feed suffix incl. dot, e.g. .TTY"),
    limit: int = Query(500, ge=1, le=20000),
) -> Response:
    fp: FeedPaths = request.app.state.feed_paths
    path = _resolve_feed_path(fp, suffix)
    parsed, rejects = _parse_feed_slice(path, suffix, limit)
    (rcols, rrows), (jcols, jrows) = parsed_records_report(parsed, rejects)
    # CSV output: single sheet — records first, then a blank line and the
    # rejects table. Excel opens this without complaint.
    csv_body = rows_to_csv(rcols, rrows)
    if jrows:
        csv_body += "\n\n"  # blank line separator
        csv_body += rows_to_csv(jcols, jrows)
    body = csv_body.encode("utf-8")
    kind = f"feed{suffix.replace('.', '-').lower()}"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{default_filename(kind, "csv")}"'
            )
        },
    )


@router.get("/api/export/feed.xlsx")
def export_feed_xlsx(
    request: Request,
    suffix: str = Query(...),
    limit: int = Query(500, ge=1, le=20000),
) -> Response:
    fp: FeedPaths = request.app.state.feed_paths
    path = _resolve_feed_path(fp, suffix)
    parsed, rejects = _parse_feed_slice(path, suffix, limit)
    (rcols, rrows), (jcols, jrows) = parsed_records_report(parsed, rejects)
    kind = f"feed{suffix.replace('.', '-').lower()}"
    sheets: list[tuple[str, Sequence[str], list[dict]]] = [
        (f"records{suffix}", rcols, rrows),
    ]
    if jrows:
        sheets.append((f"rejects{suffix}", jcols, jrows))
    return _xlsx_response(kind, sheets)


# --- Upload + inspect ------------------------------------------------------


def _sniff_suffix(filename: str, explicit: str | None) -> str:
    """Prefer the explicit form field; fall back to the upload filename."""
    if explicit:
        s = explicit if explicit.startswith(".") else f".{explicit}"
        return s.upper()
    if filename:
        # Strip anything after the last dot, e.g. RJFAF805.TTY → .TTY.
        idx = filename.rfind(".")
        if idx >= 0:
            return filename[idx:].upper()
    raise ValueError(
        "cannot determine RDG feed suffix — pass ?suffix=.TTY or upload a "
        "file whose extension is one of "
        f"{sorted(SUFFIX_HANDLERS.keys())}"
    )


_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB; feeds ship in fragments smaller than this


async def _read_upload(file: UploadFile) -> str:
    """Read the upload into a str, decoded as latin-1 to match the on-disk
    convention (feed files are ASCII in practice; latin-1 is the safe
    round-trip). Rejects oversize uploads at the boundary."""
    raw = await file.read(_UPLOAD_MAX_BYTES + 1)
    if len(raw) > _UPLOAD_MAX_BYTES:
        raise ValueError(
            f"uploaded file exceeds cap of {_UPLOAD_MAX_BYTES} bytes"
        )
    try:
        return raw.decode("latin-1")
    except Exception as exc:  # noqa: BLE001 — surface at the boundary
        raise ValueError(f"could not decode upload as latin-1: {exc}") from exc


def _parse_uploaded_body(text: str, suffix: str, limit: int
                         ) -> tuple[list[dict], list[Reject], int]:
    lines = text.splitlines()
    total = len(lines)
    if limit and limit > 0 and total > limit:
        lines = lines[:limit]
    result = inspect_lines(lines, suffix.upper())
    return result.parsed, result.rejects, total


@router.post("/api/inspect/parse")
async def inspect_parse(
    file: UploadFile = File(..., description="RDG feed file (.FFL/.TTY/…)"),
    suffix: str | None = Form(None, description="Override the suffix"),
    limit: int = Form(2000, description="Cap on lines read (0 = all up to cap)"),
) -> dict:
    """Parse an uploaded RDG feed file in-memory. Never writes to disk;
    never mutates the baseline graph. Returns a preview capped at `limit`
    lines so the browser can render the table."""
    resolved = _sniff_suffix(file.filename or "", suffix)
    text = await _read_upload(file)
    parsed, rejects, total_lines = _parse_uploaded_body(text, resolved, limit)
    return {
        "filename": file.filename,
        "suffix": resolved,
        "total_lines": total_lines,
        "parsed_count": len(parsed),
        "reject_count": len(rejects),
        "records": parsed[:200],  # UI preview only; the full parse is in the CSV
        "records_truncated": len(parsed) > 200,
        "rejects": [asdict(r) for r in rejects[:200]],
        "rejects_truncated": len(rejects) > 200,
    }


@router.post("/api/inspect/parse.csv")
async def inspect_parse_csv(
    file: UploadFile = File(...),
    suffix: str | None = Form(None),
    limit: int = Form(20000),
) -> Response:
    resolved = _sniff_suffix(file.filename or "", suffix)
    text = await _read_upload(file)
    parsed, rejects, _ = _parse_uploaded_body(text, resolved, limit)
    reject_dicts = [{"line_no": r.line_no, "reason": r.reason, "raw": r.raw}
                    for r in rejects]
    (rcols, rrows), (jcols, jrows) = parsed_records_report(parsed, reject_dicts)
    body = rows_to_csv(rcols, rrows)
    if jrows:
        body += "\n\n" + rows_to_csv(jcols, jrows)
    kind = f"upload{resolved.replace('.', '-').lower()}"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{default_filename(kind, "csv")}"'
            )
        },
    )


@router.post("/api/inspect/parse.xlsx")
async def inspect_parse_xlsx(
    file: UploadFile = File(...),
    suffix: str | None = Form(None),
    limit: int = Form(20000),
) -> Response:
    resolved = _sniff_suffix(file.filename or "", suffix)
    text = await _read_upload(file)
    parsed, rejects, _ = _parse_uploaded_body(text, resolved, limit)
    reject_dicts = [{"line_no": r.line_no, "reason": r.reason, "raw": r.raw}
                    for r in rejects]
    (rcols, rrows), (jcols, jrows) = parsed_records_report(parsed, reject_dicts)
    kind = f"upload{resolved.replace('.', '-').lower()}"
    sheets: list[tuple[str, Sequence[str], list[dict]]] = [
        (f"records{resolved}", rcols, rrows),
    ]
    if jrows:
        sheets.append((f"rejects{resolved}", jcols, jrows))
    return _xlsx_response(kind, sheets)


# --- Route SVG diagram -----------------------------------------------------


def _route_svg(path_crs: Sequence[str], names: dict[str, str],
               *, title: str, sub: str) -> str:
    """Deterministic left-to-right route strip.

    Every station in `path_crs` becomes a labelled node connected by a
    horizontal line. Colours match the cockpit's Meridian palette so the
    SVG can be pasted into the UI (or exported as-is)."""
    n = max(1, len(path_crs))
    node_gap = 92
    left_pad = 60
    right_pad = 60
    width = left_pad + (n - 1) * node_gap + right_pad
    height = 210
    y = 118
    # Labels rotate 40deg when there are >6 stops to keep them legible.
    rotate = n > 6
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'font-family="Source Sans 3, system-ui, sans-serif">'
    )
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0c0d0f"/>'
    )
    parts.append(
        f'<text x="{left_pad}" y="26" font-size="12" letter-spacing="1.4" '
        f'fill="#adb6c4" font-weight="700">{_svg_escape(title)}</text>'
    )
    parts.append(
        f'<text x="{left_pad}" y="46" font-size="10.5" letter-spacing=".6" '
        f'fill="#5f6773">{_svg_escape(sub)}</text>'
    )
    if n == 1:
        cx = left_pad
        parts.extend(_svg_stop(cx, y, path_crs[0], names.get(path_crs[0], ""), False))
    else:
        x_start = left_pad
        x_end = left_pad + (n - 1) * node_gap
        # Rail line + subtle glow.
        parts.append(
            f'<line x1="{x_start}" y1="{y}" x2="{x_end}" y2="{y}" '
            f'stroke="#2f333a" stroke-width="4" stroke-linecap="round"/>'
        )
        parts.append(
            f'<line x1="{x_start}" y1="{y}" x2="{x_end}" y2="{y}" '
            f'stroke="#6f9e86" stroke-width="1.4" stroke-dasharray="1 6" '
            f'opacity=".7"/>'
        )
        for i, crs in enumerate(path_crs):
            cx = left_pad + i * node_gap
            terminus = (i == 0 or i == n - 1)
            parts.extend(_svg_stop(cx, y, crs, names.get(crs, ""),
                                   rotate=rotate, terminus=terminus))
    parts.append("</svg>")
    return "".join(parts)


def _svg_stop(cx: int, y: int, crs: str, name: str,
              *, rotate: bool = False, terminus: bool = False) -> list[str]:
    fill = "#a9c9b7" if terminus else "#adb6c4"
    outer = "#24382e" if terminus else "#2a2e34"
    label_color = "#d8dde4" if terminus else "#98a1ae"
    lines: list[str] = []
    lines.append(
        f'<circle cx="{cx}" cy="{y}" r="7" fill="#0c0d0f" '
        f'stroke="{outer}" stroke-width="2"/>'
    )
    lines.append(f'<circle cx="{cx}" cy="{y}" r="3.4" fill="{fill}"/>')
    lines.append(
        f'<text x="{cx}" y="{y - 16}" font-size="10.5" fill="#5f6773" '
        f'text-anchor="middle" letter-spacing=".4">{_svg_escape(crs)}</text>'
    )
    if rotate:
        tx = cx
        ty = y + 22
        lines.append(
            f'<text x="{tx}" y="{ty}" font-size="10.5" fill="{label_color}" '
            f'transform="rotate(40 {tx} {ty})">{_svg_escape(name or crs)}</text>'
        )
    else:
        lines.append(
            f'<text x="{cx}" y="{y + 30}" font-size="11" fill="{label_color}" '
            f'text-anchor="middle">{_svg_escape(name or crs)}</text>'
        )
    return lines


def _svg_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _resolve_route_path(request: Request, origin: str, dest: str
                        ) -> tuple[list[str], str, int, str]:
    """Deterministic route lookup, mirroring /api/route's algorithm.
    Returns (path_crs, source, direct_trains, reason). An empty path with
    a non-empty reason is a typed miss — the caller renders that inline
    rather than fabricating a path."""
    fp: FeedPaths = request.app.state.feed_paths
    if origin == dest:
        return [], "", 0, "origin and destination are the same station"
    mca = fp.timetable_mca
    if mca is None or not mca.exists():
        return [], "", 0, "no RSPS5046 timetable (.MCA) in data/ — cannot derive a route"

    from src.ingest.timetable import load_timetable_index, trains_serving_corridor
    idx = load_timetable_index(mca)
    trains = trains_serving_corridor(idx, origin, dest)
    reversed_path = False
    a, b = origin, dest
    if not trains:
        trains = trains_serving_corridor(idx, dest, origin)
        reversed_path = True
        a, b = dest, origin
    if not trains:
        return ([], idx.source_file, 0,
                f"no through service links {origin} and {dest} in "
                f"timetable snapshot {idx.source_file}")

    best: list[str] = []
    for s in trains:
        seq = [cp.crs for cp in s.calling_points if cp.crs]
        try:
            i = seq.index(a)
            j = seq.index(b, i + 1)
        except ValueError:
            continue
        sl = seq[i:j + 1]
        if len(sl) > len(best):
            best = sl
    if reversed_path:
        best = list(reversed(best))
    return best, idx.source_file, len(trains), ""


@router.get("/api/route/svg")
def api_route_svg(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
) -> Response:
    """Inline SVG of the timetable-derived route strip. Same underlying
    lookup as `/api/route` (through-train with the most calls); the SVG
    just renders the returned CRS sequence deterministically."""
    o, d = origin.upper(), dest.upper()
    path_crs, source, direct_trains, reason = _resolve_route_path(request, o, d)
    stations = request.app.state.stations
    names = {c: (stations[c].name.title() if c in stations else c)
             for c in (path_crs or [o, d])}
    if path_crs:
        title = f"{names.get(o, o)} — {names.get(d, d)}"
        sub = f"{len(path_crs)} stops · {direct_trains} through trains · {source}"
        body = _route_svg(path_crs, names, title=title, sub=sub)
    else:
        body = _route_svg(
            [o, d],
            {o: names.get(o, o), d: names.get(d, d)},
            title=f"{o} — {d}",
            sub=reason or "no route found",
        )
    return _svg_response(body)
