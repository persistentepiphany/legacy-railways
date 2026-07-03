"""LLM fallback for the copilot — used ONLY when the grammar misses.

The model's entire job is emitting an intent JSON object per the schema in
intents.py. It never answers the question, never produces a number, never
sees engine output. Its output is schema-validated; any failure (no key,
timeout, bad JSON, unknown intent) degrades to the `help` intent.

Providers, in order: Z.AI (ZAI_API_KEY, via src.llm.zai.chat_json) then
ASI:One (ASI_ONE_API_KEY, same OpenAI-compatible shape via stdlib urllib).
No key configured → grammar-only operation, by design.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from src.copilot.grammar import Vocabulary, _corridor_sides
from src.copilot.intents import Intent, validate_intent

log = logging.getLogger(__name__)

_ASI_URL = "https://api.asi1.ai/v1/chat/completions"

_SYSTEM = """You translate an analyst's request about UK rail fares into ONE intent JSON object.
You never answer the question yourself and never invent numbers — a deterministic engine does that.

Output exactly: {"intent": <name>, "params": {...}, "confidence": <0..1>}

Intents and their params:
- resolve_fare: origin_nlc, dest_nlc (4-char NLC codes), optional ticket_code (3 chars), railcard_code (3 chars)
- explain_provenance: optional origin_nlc, dest_nlc, ticket_code — why a fare is priced the way it is
- run_impact: optional discount_pct (0-1), peak_valid (bool) — run/refresh the change's impact, or cost/revenue questions
- show_split: optional origin_nlc, dest_nlc — split-ticketing opportunities
- show_corridor: corridor_id — zoom the map to a corridor
- open_report: no params — open the impact report
- compare_fares: origin_nlc, dest_nlc, origin2_nlc, dest2_nlc, optional ticket_code
- which_breach: no params — which fares breach the regulated cap
- help: no params — anything else, greetings, or unclear requests

Known corridors (id: origin_nlc -> dest_nlc):
{corridors}

Use ONLY NLC codes shown above. If the user names a station you cannot map to
one of these NLCs, or the request is outside these intents, return
{"intent": "help", "params": {}, "confidence": 0.2}. Confidence reflects how
sure you are of the mapping. Output the JSON object only."""


def _corridor_lines(vocab: Vocabulary) -> str:
    lines = []
    for c in vocab.corridors:
        o, d = _corridor_sides(c)
        lines.append(f"- {c.get('id')}: {c.get('origin_nlc')} ({o}) -> "
                     f"{c.get('dest_nlc')} ({d})")
    return "\n".join(lines) or "- (none loaded)"


def _asi_chat_json(system: str, user: str, *, timeout_s: float = 30.0) -> dict:
    key = os.environ.get("ASI_ONE_API_KEY", "").strip()
    body = json.dumps({
        "model": "asi1-mini",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        _ASI_URL, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1])


def llm_intent(vocab: Vocabulary, text: str) -> Intent:
    """Grammar missed — ask a model for the intent JSON. Every failure mode
    lands on `help` with confidence 0; the engine is never blocked on an LLM."""
    system = _SYSTEM.replace("{corridors}", _corridor_lines(vocab))
    raw: dict | None = None
    if os.environ.get("ZAI_API_KEY", "").strip():
        try:
            from src.llm.zai import chat_json
            raw = chat_json(system, text, timeout_s=30.0)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash
            log.warning("copilot LLM (z.ai) failed: %s", exc)
    elif os.environ.get("ASI_ONE_API_KEY", "").strip():
        try:
            raw = _asi_chat_json(system, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("copilot LLM (ASI:One) failed: %s", exc)
    if raw is None:
        return Intent("help", confidence=0.0)
    return validate_intent(raw)
