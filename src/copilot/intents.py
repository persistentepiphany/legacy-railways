"""The copilot intent schema — the ONLY thing the LLM is allowed to emit.

An intent is `{"intent": <name>, "params": {...}, "confidence": 0..1}`.
`validate_intent` normalizes and hard-validates whatever the grammar or the
LLM produced; anything malformed collapses to `help` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INTENTS: frozenset[str] = frozenset({
    "resolve_fare",       # params: origin_nlc, dest_nlc, ticket_code?, railcard_code?
    "run_impact",         # params: change overrides? (discount_pct 0-1, peak_valid)
    "explain_provenance", # params: origin_nlc, dest_nlc, ticket_code?
    "show_split",         # params: origin_nlc?, dest_nlc?  (defaults to context corridor)
    "show_corridor",      # params: corridor_id
    "open_report",        # params: —
    "compare_fares",      # params: origin_nlc, dest_nlc, origin2_nlc, dest2_nlc, ticket_code?
    "which_breach",       # params: —
    "help",               # params: —
})

_PARAM_KEYS: dict[str, frozenset[str]] = {
    "resolve_fare": frozenset({"origin_nlc", "dest_nlc", "ticket_code", "railcard_code"}),
    "run_impact": frozenset({"discount_pct", "peak_valid"}),
    "explain_provenance": frozenset({"origin_nlc", "dest_nlc", "ticket_code"}),
    "show_split": frozenset({"origin_nlc", "dest_nlc"}),
    "show_corridor": frozenset({"corridor_id"}),
    "open_report": frozenset(),
    "compare_fares": frozenset({"origin_nlc", "dest_nlc", "origin2_nlc",
                                "dest2_nlc", "ticket_code"}),
    "which_breach": frozenset(),
    "help": frozenset(),
}


@dataclass(frozen=True)
class Intent:
    intent: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    # Honest surface for "I understood the shape but not the specifics" —
    # e.g. an unknown station name. Dispatch turns this into a clarification
    # answer instead of guessing.
    clarify: str | None = None


def _is_nlc(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 4 and v.isalnum()


def validate_intent(raw: Any) -> Intent:
    """Normalize grammar/LLM output into a safe Intent. Malformed → help."""
    if isinstance(raw, Intent):
        raw = {"intent": raw.intent, "params": raw.params,
               "confidence": raw.confidence, "clarify": raw.clarify}
    if not isinstance(raw, dict):
        return Intent("help", confidence=0.0)
    name = raw.get("intent")
    if name not in INTENTS:
        return Intent("help", confidence=0.0)

    params_in = raw.get("params") or {}
    if not isinstance(params_in, dict):
        params_in = {}
    allowed = _PARAM_KEYS[name]
    params: dict[str, Any] = {}
    for k, v in params_in.items():
        if k not in allowed or v is None:
            continue
        if k.endswith("_nlc") and not _is_nlc(v):
            return Intent("help", confidence=0.0)
        if k in ("ticket_code", "railcard_code"):
            if not (isinstance(v, str) and len(v) == 3 and v.isalnum()):
                continue
            v = v.upper()
        if k == "corridor_id" and not isinstance(v, str):
            continue
        if k == "discount_pct":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 1.0:      # tolerate "34" for 34%
                v = v / 100.0
            if not (0.0 < v < 1.0):
                continue
        if k == "peak_valid":
            v = bool(v)
        params[k] = v

    try:
        conf = float(raw.get("confidence", 1.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    clarify = raw.get("clarify")
    if clarify is not None and not isinstance(clarify, str):
        clarify = None
    return Intent(name, params, conf, clarify)
