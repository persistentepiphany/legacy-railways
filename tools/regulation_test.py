"""Stitch BRFares fetch (Part A) + feed classifier (Part B) into a single
markdown comparison.

Reads:
    data/brfares_man_eus.json         (from tools/fetch_brfares.py)
    data/brfares_sot_man.json
    data/classification_corridor.json (from tools/classify_corridor.py)

Writes:
    docs/regulation-test-results.md

Eyeball the MATCH column: all five §5 cases ✅ -> compliance feature trustworthy.

Run from the repo root, after Parts A and B:

    python tools/regulation_test.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"


# Mapping: §5 case -> which BRFares snapshot to read.
# Keep this in sync with tools/classify_corridor.py CASES list.
BRFARES_BY_CORRIDOR = {
    "MAN-EUS": DATA_DIR / "brfares_man_eus.json",
    "SOT-MAN": DATA_DIR / "brfares_sot_man.json",
}

# Coarse map: case-name substring -> (corridor, expected regulation per §5).
# Used to bind classification rows back to a BRFares snapshot. Keys must
# uniquely identify each case row.
CASE_BINDING = {
    "MAN<->EUS Off-Peak Return":   ("MAN-EUS", "Regulated"),
    "SOT<->MAN Off-Peak Return":   ("SOT-MAN", "Regulated"),
    "MAN<->EUS Anytime Return":    ("MAN-EUS", "NOT regulated"),
    "MAN<->EUS Advance":           ("MAN-EUS", "NOT regulated"),
    "MAN<->EUS First Class Rtn":   ("MAN-EUS", "NOT regulated"),
}


def _pounds_to_pence(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if "." in str(value):
        return int((d * 100).to_integral_value())
    return int((d * 100).to_integral_value())


def _get_path(d: Any, *path: str) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def index_brfares(payload: Any) -> dict[str, int]:
    """ticket_code -> pence, taking the cheapest match if duplicated."""
    fares = payload.get("fares") if isinstance(payload, dict) else payload
    if not isinstance(fares, list):
        fares = _get_path(payload, "data", "fares") or []
    out: dict[str, int] = {}
    for f in fares:
        code = _get_path(f, "ticket", "code") or f.get("ticket_code")
        if not code:
            continue
        raw_price = f.get("fare") if "fare" in f else f.get("adult_fare")
        pence = _pounds_to_pence(raw_price)
        if pence is None:
            continue
        prev = out.get(code)
        if prev is None or pence < prev:
            out[code] = pence
    return out


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"warning: {path} is not valid JSON ({exc}); treating as missing", file=sys.stderr)
        return None


def verdict(
    feed_fare: int | None,
    brf_fare: int | None,
    feed_class: str,
    expected_class: str,
) -> str:
    if feed_fare is None or brf_fare is None:
        return "⚠️ pending"
    if feed_class != expected_class and feed_class != "MISSING":
        return f"❌ classified {feed_class}, §5 expects {expected_class}"
    if feed_class == "MISSING":
        return "⚠️ pending (feed gap)"
    if feed_fare != brf_fare:
        return f"❌ fare mismatch: feed {feed_fare} vs BRFares {brf_fare}"
    return "✅"


def render_markdown(rows: list[dict[str, Any]], sources: dict[str, Path | None]) -> str:
    today = dt.date.today().isoformat()
    out: list[str] = []
    out.append("# Regulation test — §5 corridor cases")
    out.append("")
    out.append(
        f"Generated: {today}. Compares the feed-side classifier "
        f"(`tools/classify_corridor.py`) against the BRFares legacy JSON oracle "
        f"(`tools/fetch_brfares.py`)."
    )
    out.append("")
    out.append("| Case | Ticket | Feed fare (p) | BRFares fare (p) | Regulated? | §5 expects | Rule fired | MATCH? |")
    out.append("|---|---|---:|---:|---|---|---|---|")
    for r in rows:
        feed_s = str(r["feed_fare"]) if r["feed_fare"] is not None else "—"
        brf_s = str(r["brf_fare"]) if r["brf_fare"] is not None else "—"
        out.append(
            f"| {r['case']} | `{r['ticket_code']}` | {feed_s} | {brf_s} | "
            f"{r['classification']} | {r['expected']} | {r['rule']} | {r['match']} |"
        )
    out.append("")
    out.append("## Sources")
    for label, path in sources.items():
        if path is None:
            out.append(f"- **{label}** — _missing_; re-run the producer script.")
        else:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            out.append(f"- **{label}** — `{path.relative_to(REPO_ROOT)}` (mtime {mtime})")
    out.append("")
    return "\n".join(out)


def run() -> int:
    cls_path = DATA_DIR / "classification_corridor.json"
    classification = load_json(cls_path)
    if classification is None:
        print(
            f"error: {cls_path} is missing. Run tools/classify_corridor.py first.",
            file=sys.stderr,
        )
        return 2

    brfares: dict[str, dict[str, int]] = {}
    brfares_sources: dict[str, Path | None] = {}
    for corridor, path in BRFARES_BY_CORRIDOR.items():
        payload = load_json(path)
        brfares_sources[f"BRFares {corridor}"] = path if payload is not None else None
        brfares[corridor] = index_brfares(payload) if payload is not None else {}

    rows: list[dict[str, Any]] = []
    for cls_row in classification.get("results", []):
        case_name = cls_row["case"]
        binding = CASE_BINDING.get(case_name)
        if binding is None:
            # Unknown case key — surface it rather than silently dropping.
            corridor, expected = "?", "?"
            brf_fare = None
        else:
            corridor, expected = binding
            brf_fare = brfares.get(corridor, {}).get(cls_row["ticket_code"])

        rows.append({
            "case": case_name,
            "ticket_code": cls_row["ticket_code"],
            "feed_fare": cls_row["fare_pence"],
            "brf_fare": brf_fare,
            "classification": cls_row["classification"],
            "expected": expected,
            "rule": cls_row["rule"],
            "match": verdict(
                cls_row["fare_pence"], brf_fare,
                cls_row["classification"], expected,
            ),
        })

    sources: dict[str, Path | None] = {"Classifier": cls_path if classification else None}
    sources.update(brfares_sources)

    md = render_markdown(rows, sources)
    out_path = DOCS_DIR / "regulation-test-results.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"wrote {out_path.relative_to(REPO_ROOT)}")

    failing = [r for r in rows if not r["match"].startswith("✅")]
    if failing:
        print(f"  {len(failing)} of {len(rows)} rows are not ✅ — see the table.")
    else:
        print(f"  all {len(rows)} rows ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
