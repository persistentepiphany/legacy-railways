"""CSV / XLSX report exporters for the fares cockpit.

These helpers produce deterministic, byte-stable exports of the values the
cockpit already displays: baseline corridor fares, the staging queue, a
free-form corridor's flow-fare table, and — for any uploaded or on-disk
RDG feed file — the parsed record table plus its quarantined rejects.

Nothing here computes a new number: builders read the same indexes and the
same staging layer the JSON endpoints already serve, so a CSV row and a UI
row for the same corridor cannot disagree. Every column has a stable order,
every cell is a plain string — a diff against a fresh export at a later
snapshot is meaningful.

XLSX writer:
    The `write_xlsx_bytes` helper hand-emits the small subset of the Office
    Open XML SpreadsheetML wire format that Excel and LibreOffice both
    accept — no `openpyxl`/`xlsxwriter` dependency. Every cell uses inline
    strings (t="inlineStr"); we don't bother with the sharedStrings table
    because these exports are read once and not re-opened for editing.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone
from typing import Iterable, Sequence
from xml.sax.saxutils import escape as _xml_escape


# --- CSV -------------------------------------------------------------------


def rows_to_csv(columns: Sequence[str], rows: Iterable[dict]) -> str:
    """Emit a UTF-8 CSV with the given header, in row order.
    Missing cells become empty strings — never `None`, so opening the file
    in Excel doesn't render the literal `None`."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow(["" if row.get(c) is None else str(row.get(c)) for c in columns])
    return buf.getvalue()


# --- XLSX ------------------------------------------------------------------
# Hand-emitted Office Open XML — small enough that adding a dep isn't worth
# the deploy overhead. Every worksheet is one <sheetData> with inline
# strings; there are no formulas, styles, merged cells, or shared strings.


_XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheet_overrides}
</Types>"""

_XLSX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _clean_sheet_name(raw: str) -> str:
    """Excel forbids these chars in a sheet name and caps length at 31."""
    for bad in "/\\?*[]:":
        raw = raw.replace(bad, "_")
    return (raw or "sheet")[:31]


def _clean_cell(value: object) -> str:
    """Excel worksheet content can't hold ASCII control chars < 0x20
    (except TAB/LF/CR). Strip everything else so a stray record separator
    from a raw feed line doesn't corrupt the workbook."""
    if value is None:
        return ""
    s = str(value)
    return "".join(c for c in s if c >= " " or c in "\t\n\r")


def write_xlsx_bytes(sheets: Sequence[tuple[str, Sequence[str], Iterable[dict]]]) -> bytes:
    """Bytes of a minimal, valid `.xlsx` file with the given sheets.

    Each `sheets` entry is `(name, columns, rows)` — same shape as the CSV
    inputs. Sheet names are truncated to Excel's 31-char cap and stripped
    of illegal characters; ASCII control characters are stripped per cell.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        overrides: list[str] = []
        sheet_refs: list[str] = []
        wb_rels: list[str] = []
        for idx, (raw_name, columns, rows) in enumerate(sheets, start=1):
            name = _clean_sheet_name(raw_name)
            overrides.append(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.worksheet+xml"/>'
            )
            sheet_refs.append(
                f'<sheet name="{_xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
            )
            wb_rels.append(
                f'<Relationship Id="rId{idx}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                f'relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            )
            z.writestr(
                f"xl/worksheets/sheet{idx}.xml",
                _worksheet_xml(columns, rows),
            )

        z.writestr(
            "[Content_Types].xml",
            _XLSX_CONTENT_TYPES.format(sheet_overrides="\n".join(overrides)),
        )
        z.writestr("_rels/.rels", _XLSX_ROOT_RELS)
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>" + "".join(sheet_refs) + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(wb_rels) + "</Relationships>",
        )
    return buf.getvalue()


def _col_ref(index: int) -> str:
    """0-based column index → Excel letter reference (A, B, ..., Z, AA, ...)."""
    ref = ""
    n = index
    while True:
        ref = chr(ord("A") + n % 26) + ref
        n = n // 26 - 1
        if n < 0:
            break
    return ref


def _worksheet_xml(columns: Sequence[str], rows: Iterable[dict]) -> str:
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    # Header row.
    parts.append('<row r="1">')
    for ci, col in enumerate(columns):
        ref = f"{_col_ref(ci)}1"
        parts.append(
            f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
            f"{_xml_escape(_clean_cell(col))}</t></is></c>"
        )
    parts.append("</row>")

    for ri, row in enumerate(rows, start=2):
        parts.append(f'<row r="{ri}">')
        for ci, col in enumerate(columns):
            ref = f"{_col_ref(ci)}{ri}"
            cell = _clean_cell(row.get(col))
            if cell == "":
                # Emit the ref with no value so Excel keeps the column layout.
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t/></is></c>')
            else:
                parts.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                    f"{_xml_escape(cell)}</t></is></c>"
                )
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


# --- Report builders -------------------------------------------------------
# Each builder returns `(columns, rows)` where `rows` is a list of dicts
# keyed by column name. The columns list decides ordering — the dict itself
# may hold extra keys (ignored by the writers).


CORRIDOR_COLUMNS: list[str] = [
    "id", "name", "toc",
    "origin_crs", "origin_nlc",
    "dest_crs", "dest_nlc",
    "default_ticket", "default_ticket_regulated",
    "default_ticket_price_pence",
    "cheapest_ticket", "cheapest_ticket_price_pence",
    "dearest_ticket", "dearest_ticket_price_pence",
    "fares_scanned", "aberration_count",
    "train_count",
    "odm_journeys_out", "odm_journeys_back",
    "distance_km", "distance_method",
    "rail_kgco2e_per_journey", "car_kgco2e_per_journey",
    "carbon_saving_per_journey_kg", "annual_carbon_saving_kg",
    "implied_yield_pence",
    "notes",
]


def _key_fare(row: dict, label: str) -> tuple[str, int | None]:
    for kf in row.get("key_fares") or ():
        if kf.get("label") == label:
            return kf.get("ticket_code", ""), kf.get("price_pence")
    return "", None


def corridor_report(overview_cache: dict | None) -> tuple[list[str], list[dict]]:
    """Baseline-corridor summary — one row per curated corridor, matching
    what `/api/overview` shows. Returns empty rows if the overview isn't
    warm yet (caller should surface a 400 in that case)."""
    if overview_cache is None:
        return CORRIDOR_COLUMNS, []
    out: list[dict] = []
    for row in overview_cache.get("rows", []):
        def_tc, def_p = _key_fare(row, "default")
        cheap_tc, cheap_p = _key_fare(row, "cheapest")
        dear_tc, dear_p = _key_fare(row, "dearest")
        out.append({
            "id": row["id"],
            "name": row["name"],
            "toc": row.get("toc") or "",
            "origin_crs": row["origin_crs"],
            "origin_nlc": row["origin_nlc"],
            "dest_crs": row["dest_crs"],
            "dest_nlc": row["dest_nlc"],
            "default_ticket": def_tc,
            "default_ticket_regulated": (
                "" if row.get("default_ticket_regulated") is None
                else str(row.get("default_ticket_regulated")).lower()
            ),
            "default_ticket_price_pence": def_p,
            "cheapest_ticket": cheap_tc,
            "cheapest_ticket_price_pence": cheap_p,
            "dearest_ticket": dear_tc,
            "dearest_ticket_price_pence": dear_p,
            "fares_scanned": row.get("fares_scanned"),
            "aberration_count": row.get("aberration_count"),
            "train_count": row.get("train_count"),
            "odm_journeys_out": row.get("odm_journeys_out"),
            "odm_journeys_back": row.get("odm_journeys_back"),
            "distance_km": row.get("distance_km"),
            "distance_method": row.get("distance_method"),
            "rail_kgco2e_per_journey": row.get("rail_kgco2e_per_journey"),
            "car_kgco2e_per_journey": row.get("car_kgco2e_per_journey"),
            "carbon_saving_per_journey_kg": row.get("carbon_saving_per_journey_kg"),
            "annual_carbon_saving_kg": row.get("annual_carbon_saving_kg"),
            "implied_yield_pence": row.get("implied_yield_pence"),
            "notes": " | ".join(row.get("notes") or ()),
        })
    return CORRIDOR_COLUMNS, out


FARES_COLUMNS: list[str] = [
    "flow_id", "ticket_code", "route_code",
    "representative_origin_nlc", "representative_dest_nlc",
    "origin_name", "dest_name",
    "price_pence", "price_pounds",
    "discount_category",
    "blast_radius_pair_count",
    "provenance_summary",
]


def fares_report(affected: Iterable, origin_name: str = "",
                 dest_name: str = "") -> tuple[list[str], list[dict]]:
    """One row per baseline fare on a corridor. `affected` is the
    `baseline_affected()` output (a tuple of `AffectedFare`)."""
    out: list[dict] = []
    for f in affected:
        price = f.old_price_pence
        out.append({
            "flow_id": f.flow_id,
            "ticket_code": f.ticket_code,
            "route_code": f.route_code,
            "representative_origin_nlc": f.representative_origin_nlc,
            "representative_dest_nlc": f.representative_dest_nlc,
            "origin_name": f.representative_origin_name or origin_name,
            "dest_name": f.representative_dest_name or dest_name,
            "price_pence": price,
            "price_pounds": (
                "" if price is None else f"{price / 100:.2f}"
            ),
            "discount_category": f.discount_category,
            "blast_radius_pair_count": len(f.blast_radius_pairs),
            "provenance_summary": " → ".join(p.step for p in f.provenance),
        })
    return FARES_COLUMNS, out


STAGING_COLUMNS: list[str] = [
    "card_id", "status", "kind", "description",
    "corridor_origin_nlc", "corridor_dest_nlc",
    "scope", "toc_code",
    "peak_valid", "railcard_code", "ticket_code",
    "discount_pct", "affected_count",
    "revenue_delta_pence", "breach_count",
]


def staging_report(layer) -> tuple[list[str], list[dict]]:
    """The current staging layer as a table — pending first, then approved.
    Mirrors what the right-hand queue panel already shows."""
    out: list[dict] = []
    def _add(cards, status: str) -> None:
        for card in cards:
            ch = card.change
            impact = card.impact
            rev = getattr(impact, "revenue", None)
            comp = getattr(impact, "compliance", None)
            out.append({
                "card_id": card.card_id,
                "status": status,
                "kind": ch.kind,
                "description": ch.description,
                "corridor_origin_nlc": ch.corridor_origin_nlc,
                "corridor_dest_nlc": ch.corridor_dest_nlc,
                "scope": getattr(ch, "scope", "corridor"),
                "toc_code": getattr(ch, "toc_code", "") or "",
                "peak_valid": "yes" if ch.peak_valid else "no",
                "railcard_code": getattr(ch, "railcard_code", "") or "",
                "ticket_code": getattr(ch, "ticket_code", "") or "",
                "discount_pct": getattr(ch, "discount_pct", ""),
                "affected_count": len(impact.canonical_affected),
                "revenue_delta_pence": (
                    getattr(rev, "aggregate_delta_pence", "") if rev else ""
                ),
                "breach_count": (
                    getattr(comp, "breach_count", "") if comp else ""
                ),
            })
    _add(layer.pending, "pending")
    _add(layer.approved, "approved")
    return STAGING_COLUMNS, out


# --- Feed-file parse export ------------------------------------------------


def parsed_records_report(
    parsed: list[dict[str, str]],
    rejects: list[dict],
) -> tuple[tuple[list[str], list[dict]], tuple[list[str], list[dict]]]:
    """Two related tables from an inspected feed file:
        - `(record_columns, record_rows)` — parsed record fields
        - `(reject_columns, reject_rows)` — quarantined lines + reasons

    Column order is the natural insertion order of the first parsed record;
    later records with extra keys have those keys appended in stable order.
    """
    seen: dict[str, None] = {}
    for row in parsed:
        for k in row.keys():
            seen.setdefault(k, None)
    record_columns = list(seen.keys())
    record_rows = [dict(row) for row in parsed]

    reject_columns = ["line_no", "reason", "raw"]
    return (record_columns, record_rows), (reject_columns, rejects)


def default_filename(kind: str, suffix: str) -> str:
    """`<kind>-<UTC-timestamp>.<suffix>` — safe on every OS."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{ts}.{suffix}"
