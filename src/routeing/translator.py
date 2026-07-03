"""Build-time easement-text translator (LLM touchpoint, offline only).

Reads .RGE (RSPS5047 § 6.16 free-form English, up to 2000 chars per
easement) and produces `data/easement_predicates.json` — a per-easement
structured summary the runtime engine can display alongside the .RGF
structured predicates.

Purpose (why bother when .RGF is already structured?):

  * .RGE English carries the *intent* — an analyst reading a firing
    easement wants to see "customers may change at Norwich" not just
    L,000509,NRW,1.
  * The LLM can cross-check the English against the .RGF structure and
    flag data-quality issues (e.g. English says "Avanti only" but .RGF
    has no D-record for TOC=VT).
  * The English mentions station NAMES; .RGF uses CRS codes.  Bridging
    these enables the UI to render friendly station labels.

Discipline (per CLAUDE.md):

  * The LLM NEVER computes a fare and NEVER decides validity at runtime.
    This script is the third LLM touchpoint, and it runs offline.
  * Deterministic: `temperature=0` and the prompt asks for JSON with a
    fixed schema so repeat runs are stable.
  * Cached: if `easement_predicates.json` already contains a text_ref,
    we skip it (idempotent runs across snapshots).
  * Failure isolated: a single ZaiError skips that easement with a note;
    the rest of the batch continues.

Usage:

    python -m src.routeing.translator \\
        --rge data/RJRG0117.RGE \\
        --out data/easement_predicates.json \\
        [--limit 10]                  # translate only N; --limit 0 = all
        [--refresh EASEMENT_TEXT_REF] # force-retranslate one text_ref
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from src.ingest.routeing import load_easement_texts
from src.llm.zai import ZaiError, chat_json


def _load_dotenv() -> None:
    """Populate os.environ from ./.env for CLI invocations.

    Mirrors src.api.main._load_dotenv so the translator picks up
    ZAI_API_KEY without the caller having to export it manually."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip(); v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


PROMPT_SYSTEM = (
    "You translate a UK National Routeing Guide (NRG) easement description "
    "into a structured JSON summary. You do NOT decide whether the easement "
    "applies to any journey — that decision is made by a deterministic "
    "engine using the parallel .RGF structured records. Your only job is "
    "to make the free-form English legible for a rail-fares analyst.\n\n"
    "Return STRICT JSON matching exactly this schema (no keys added, no "
    "keys omitted; use null or empty arrays where nothing applies):\n"
    "{\n"
    '  "summary": string,                      // one sentence, plain English\n'
    '  "intent": "grant" | "restrict" | "clarify",\n'
    '  "origin_stations": [string],            // station NAMES mentioned as origin\n'
    '  "destination_stations": [string],       // NAMES mentioned as destination\n'
    '  "via_stations": [string],               // NAMES mentioned as via/interchange\n'
    '  "excluded_stations": [string],          // NAMES mentioned as excluded\n'
    '  "tocs_mentioned": [string],             // operator names or codes (e.g. "LNER", "VT")\n'
    '  "ticket_types_mentioned": [string],     // e.g. "Advance", "Sleeper", "Anytime"\n'
    '  "time_constraints": string | null,      // free-form time or day-of-week text if any\n'
    '  "ambiguity_flags": [string]             // anything you found unclear or self-contradictory\n'
    "}\n\n"
    "Be conservative in ambiguity_flags — flag genuine unclarity, not "
    "normal legalese. If the English uses a station name you can't confidently "
    "identify, include it verbatim rather than guessing a canonical name."
)


def translate_batch(
    rge_path: Path,
    out_path: Path,
    *,
    limit: int = 0,
    refresh_refs: frozenset[str] = frozenset(),
    sleep_between_s: float = 0.0,
) -> dict[str, Any]:
    """Translate every easement text into predicates JSON, cached to disk.

    `limit == 0` means translate everything not already cached.
    `refresh_refs` forces re-translation of specific text_refs even if
    they're already in the cache — useful when refining the prompt."""
    texts = load_easement_texts(rge_path)
    cache = _load_cache(out_path)

    to_do: list[str] = []
    for ref in texts:
        if ref in refresh_refs:
            to_do.append(ref)
        elif ref not in cache:
            to_do.append(ref)
    if limit > 0:
        to_do = to_do[:limit]

    print(
        f"translator: {len(texts)} easement texts in {rge_path.name}; "
        f"{len(cache)} already cached; translating {len(to_do)} now.",
        file=sys.stderr,
    )

    for i, ref in enumerate(to_do, start=1):
        text = texts[ref].text
        try:
            result = _translate_one(ref, text)
        except ZaiError as e:
            print(f"  [{i}/{len(to_do)}] {ref}: SKIP ({e})", file=sys.stderr)
            cache[ref] = {"error": str(e), "text": text}
            continue
        cache[ref] = result
        print(
            f"  [{i}/{len(to_do)}] {ref}: {result.get('summary', '')[:80]}",
            file=sys.stderr,
        )
        # Persist incrementally so a mid-run crash doesn't lose progress.
        _atomic_write(out_path, cache)
        if sleep_between_s > 0:
            time.sleep(sleep_between_s)

    _atomic_write(out_path, cache)
    return cache


def _translate_one(text_ref: str, text: str) -> dict[str, Any]:
    """Call ZAI once; return the parsed JSON dict.  Raises ZaiError on
    auth / network / non-JSON responses (caller handles per-ref failure)."""
    user_msg = (
        f"Easement TEXT_REF: {text_ref}\n"
        f"Easement English text:\n{text.strip()}\n\n"
        "Return the structured JSON per the schema."
    )
    return chat_json(PROMPT_SYSTEM, user_msg)


def _load_cache(out_path: Path) -> dict[str, Any]:
    if not out_path.exists():
        return {}
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupted cache — back it up and start fresh rather than crash.
        backup = out_path.with_suffix(out_path.suffix + ".corrupt")
        out_path.rename(backup)
        print(f"translator: cache at {out_path} was corrupt; backed up to {backup}",
              file=sys.stderr)
        return {}


def _atomic_write(out_path: Path, cache: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(out_path)


def _main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rge", required=True, type=Path, help="Path to RJRG*.RGE")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output JSON cache (typically data/easement_predicates.json)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Translate at most N un-cached refs (0 = all)")
    ap.add_argument("--refresh", action="append", default=[],
                    help="Force-refresh specific text_ref (repeatable)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="Seconds to sleep between calls (rate-limit protection)")
    args = ap.parse_args()

    if not args.rge.exists():
        print(f"error: RGE file not found: {args.rge}", file=sys.stderr)
        return 2

    translate_batch(
        args.rge, args.out,
        limit=args.limit,
        refresh_refs=frozenset(args.refresh),
        sleep_between_s=args.sleep,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
