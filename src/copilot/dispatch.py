"""Dispatch — the copilot's ONLY answer path.

`answer()` turns text into a typed intent (grammar first, LLM fallback only on
a miss), then calls the SAME deterministic engine functions the API endpoints
use — read-only — and fills fixed English templates with the engine's numbers
verbatim. The LLM never contributes a number, a price, or a sentence of prose
to any answer (CLAUDE.md discipline).

ui_commands speak the window-CustomEvent contract the cockpit listens on
(meridian:zoomToCorridor, meridian:highlightStations, ...). The drawer replays
them; curl and agent callers simply ignore them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.copilot.grammar import Vocabulary, parse
from src.copilot.intents import Intent
from src.copilot.llm import llm_intent
from src.impact.change_request import ChangeRequest
from src.impact.feed_paths import FeedPaths
from src.impact.report import DEFAULT_INCLUDE, ImpactReport, compute_impact
from src.resolver.resolve import ResolvedFare, resolve_fare

log = logging.getLogger(__name__)

_DEFAULT_TICKET = "SOR"  # Anytime Return per .TTY — the cockpit's demo default
_DEFAULT_RAILCARD_NAME = "Student Railcard"

HELP_TEXT = (
    "I answer from the deterministic fares engine — I never invent a number. Try:\n"
    "\u2022 fare from Manchester to London Euston\n"
    "\u2022 why is it that price\n"
    "\u2022 run the impact\n"
    "\u2022 which fares breach the cap\n"
    "\u2022 show the splits"
)

_MISS_TEXT = {
    "no_flow": "No flow record links {o} \u2192 {d} in the loaded feed \u2014 "
               "the feed carries no fare for this pair.",
    "no_fare": "A flow exists for {o} \u2192 {d}, but it carries no {t} fare.",
    "suppressed": "{o} \u2192 {d} {t} is suppressed by an .NFO override "
                  "(99999999 sentinel) \u2014 intentionally not on sale.",
    "ambiguous": "The feed is ambiguous for {o} \u2192 {d} {t}; the resolver "
                 "escalates instead of guessing. The provenance chain shows "
                 "the conflicting records.",
    "contradiction": "Contradictory .NFO overrides exist for {o} \u2192 {d} {t} "
                     "\u2014 escalated for human review, never auto-resolved.",
}


@dataclass
class CopilotState:
    """Built once per process from app.state (see src/api/copilot.py)."""
    fp: FeedPaths
    vocab: Vocabulary
    names: dict[str, str] = field(default_factory=dict)  # NLC → display name
    impact_cache: dict[tuple, ImpactReport] = field(default_factory=dict)


# --- Small helpers ----------------------------------------------------------


def _gbp(pence: int | None) -> str:
    # None never happens on the paths we render (resolved fares / breach rows
    # carry prices by construction) — "—" keeps the template honest if it did.
    return "\u2014" if pence is None else f"\u00a3{pence / 100:,.2f}"


def _name(state: CopilotState, nlc: str) -> str:
    return state.names.get(nlc, nlc)


def _proposal_code(name: str) -> str:
    """Mirror of fare-engine.js `_proposalCode` — same synthetic railcard code
    as the cockpit so copilot and UI runs share one staging identity."""
    h = 7
    for ch in name:
        h = (h * 31 + ord(ch)) % 100
    return f"Z{h:02d}"


def _ticket_desc(state: CopilotState, code: str) -> str:
    from src.ingest.inspect import load_ticket_type_meta
    rec = load_ticket_type_meta(state.fp.tty).get(code)
    return f"{code} ({rec.description.strip().title()})" if rec else code


def _context_corridor(state: CopilotState, context: dict) -> dict | None:
    cid = (context or {}).get("corridor_id")
    for c in state.vocab.corridors:
        if c.get("id") == cid:
            return c
    return dict(state.vocab.corridors[0]) if state.vocab.corridors else None


def _corridor_by_id(state: CopilotState, cid: str) -> dict | None:
    for c in state.vocab.corridors:
        if c.get("id") == cid:
            return c
    return None


def _resolve(state: CopilotState, origin: str, dest: str, ticket: str,
             railcard: str | None = None) -> ResolvedFare:
    fp = state.fp
    return resolve_fare(
        origin, dest, ticket, fp.ffl,
        loc_path=fp.loc, fsc_path=fp.fsc, nfo_path=fp.nfo,
        rlc_path=fp.rlc, dis_path=fp.dis, rcm_path=fp.rcm,
        frr_path=fp.frr, tty_path=fp.tty,
        railcard_code=railcard, on_date=date.today(),
    )


def _change(state: CopilotState, context: dict, *,
            discount_pct: float | None = None, peak_valid: bool | None = None,
            origin_nlc: str | None = None, dest_nlc: str | None = None,
            ) -> ChangeRequest:
    """The cockpit's default proposal (Student Railcard, 34%, off-peak),
    mirroring frontend buildChangeRequest() field for field, with optional
    per-query overrides. Read-only input to compute_impact — never staged."""
    c = _context_corridor(state, context)
    o = origin_nlc or (c or {}).get("origin_nlc")
    d = dest_nlc or (c or {}).get("dest_nlc")
    if not o or not d:
        raise ValueError("no corridor loaded to run the change against")
    pct = discount_pct if discount_pct is not None else 0.34
    return ChangeRequest(
        kind="add_railcard",
        railcard_code=_proposal_code(_DEFAULT_RAILCARD_NAME),
        discount_pct=pct,
        discount_categories=("01",),
        corridor_origin_nlc=o,
        corridor_dest_nlc=d,
        peak_valid=bool(peak_valid) if peak_valid is not None else False,
        description=f"{_DEFAULT_RAILCARD_NAME} \u00b7 {round(pct * 100)}%",
        rounding_rule="near10",
        min_floor_pct=0.55,
        cluster_name="national",
    )


def _impact(state: CopilotState, change: ChangeRequest,
            include: frozenset[str]) -> ImpactReport:
    key = (change.railcard_code, change.discount_pct, change.peak_valid,
           change.corridor_origin_nlc, change.corridor_dest_nlc, include)
    hit = state.impact_cache.get(key)
    if hit is None:
        hit = compute_impact(change, state.fp, include=include)
        if len(state.impact_cache) >= 16:
            state.impact_cache.pop(next(iter(state.impact_cache)))
        state.impact_cache[key] = hit
    return hit


def _ui(event: str, payload: dict | None = None) -> dict:
    return {"event": f"meridian:{event}", "payload": payload or {}}


# --- Per-intent handlers ----------------------------------------------------
# Each returns (answer_text, ui_commands). Numbers come from the engine
# verbatim; the templates are fixed English.


def _h_resolve(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    c = _context_corridor(state, context) or {}
    o = p.get("origin_nlc") or c.get("origin_nlc")
    d = p.get("dest_nlc") or c.get("dest_nlc")
    if not o or not d:
        return ("Which stations? Try \u201cfare from Manchester to London "
                "Euston\u201d.", [])
    ticket = p.get("ticket_code", _DEFAULT_TICKET)
    rc = p.get("railcard_code")
    r = _resolve(state, o, d, ticket, rc)
    on, dn = _name(state, o), _name(state, d)
    ui = [_ui("highlightStations", {"nlcs": [o, d], "pulse": True}),
          _ui("openTab", {"tab": "blast"})]
    if r.status != "resolved":
        return _MISS_TEXT[r.status].format(o=on, d=dn, t=ticket), ui
    rc_txt = f" with railcard {rc}" if rc else ""
    text = (f"{on} \u2192 {dn}, {_ticket_desc(state, ticket)}{rc_txt}: "
            f"{_gbp(r.price_pence)}. Resolved deterministically in "
            f"{len(r.provenance)} steps \u2014 ask \u201cwhy is it that "
            "price\u201d for the chain.")
    return text, ui


def _h_explain(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    c = _context_corridor(state, context) or {}
    o = p.get("origin_nlc") or c.get("origin_nlc")
    d = p.get("dest_nlc") or c.get("dest_nlc")
    if not o or not d:
        return ("Which fare? Try \u201cwhy does the fare from Manchester to "
                "London Euston cost that\u201d.", [])
    ticket = p.get("ticket_code", _DEFAULT_TICKET)
    r = _resolve(state, o, d, ticket)
    on, dn = _name(state, o), _name(state, d)
    if r.status == "resolved":
        head = (f"{_gbp(r.price_pence)} for {_ticket_desc(state, ticket)} "
                f"{on} \u2192 {dn} \u2014 the deterministic chain:")
    else:
        head = _MISS_TEXT[r.status].format(o=on, d=dn, t=ticket) + " The chain:"
    steps = [f"{i}. {s.step} \u2014 {s.source}"
             for i, s in enumerate(r.provenance, 1)]
    if len(steps) > 14:
        extra = len(steps) - 14
        steps = steps[:14] + [f"\u2026 {extra} more steps (full chain in the "
                              "resolved-fare panel)."]
    ui = [_ui("highlightStations", {"nlcs": [o, d], "pulse": True})]
    return head + "\n" + "\n".join(steps), ui


def _h_impact(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    change = _change(state, context, discount_pct=p.get("discount_pct"),
                     peak_valid=p.get("peak_valid"))
    rep = _impact(state, change, DEFAULT_INCLUDE)
    c = _context_corridor(state, context) or {}
    where = c.get("name") or (f"{_name(state, change.corridor_origin_nlc)} "
                              f"\u2192 {_name(state, change.corridor_dest_nlc)}")
    lines = [
        f"Impact of {change.description} on {where}:",
        f"\u2022 {len(rep.canonical_affected)} fares repriced"
        + (f" ({len(rep.skipped)} skipped)" if rep.skipped else "")
        + f", blast radius {len(rep.blast_radius_pairs)} station pairs.",
    ]
    if rep.compliance is not None:
        lines.append(f"\u2022 Compliance: {rep.compliance.regulated_count} "
                     f"regulated, {rep.compliance.breach_count} breach(es) of "
                     "the 1 March 2025 cap.")
    if rep.anomalies is not None:
        lines.append(f"\u2022 Anomalies: {len(rep.anomalies.inversions)} fare "
                     "inversion(s).")
    if rep.revenue is not None:
        lines.append(f"\u2022 Revenue exposure (per-flow): "
                     f"{_gbp(rep.revenue.per_flow_exposure_pence)} \u2014 "
                     "structural exposure (EST), not a forecast.")
    params: dict[str, Any] = {}
    if p.get("discount_pct") is not None:
        params["discountPct"] = round(change.discount_pct * 100)
    if p.get("peak_valid") is not None:
        params["peakOn"] = change.peak_valid
    ui = [_ui("runImpact", {"changeParams": params} if params else {}),
          _ui("openTab", {"tab": "blast"})]
    return "\n".join(lines), ui


def _h_breach(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    rep = _impact(state, _change(state, context), DEFAULT_INCLUDE)
    cb = rep.compliance
    ui = [_ui("openTab", {"tab": "compliance"})]
    if cb is None:
        return "The compliance block was not computed for this change.", ui
    if cb.regulated_count == 0:
        return ("No regulated fares in this change's affected set \u2014 the "
                "1 March 2025 cap does not bind here.", ui)
    if cb.breach_count == 0:
        return (f"No breaches: all {cb.regulated_count} regulated fares stay "
                "at or under their 1 March 2025 cap under this change.", ui)
    lines = [f"{cb.breach_count} of {cb.regulated_count} regulated fares "
             "breach the cap:"]
    for f in cb.breaches[:3]:
        v = f.compliance
        if v is None:  # breach rows always carry a verdict by construction
            continue
        on = f.representative_origin_name or _name(state, f.representative_origin_nlc)
        dn = f.representative_dest_name or _name(state, f.representative_dest_nlc)
        lines.append(f"\u2022 {f.ticket_code} {on} \u2192 {dn}: "
                     f"{_gbp(f.old_price_pence)} \u2192 {_gbp(f.new_price_pence)} "
                     f"vs cap {_gbp(v.cap_price_2025_pence)} ({v.citation})")
    if cb.breach_count > 3:
        lines.append(f"\u2026 and {cb.breach_count - 3} more in the compliance tab.")
    return "\n".join(lines), ui


def _h_split(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    change = _change(state, context, origin_nlc=p.get("origin_nlc"),
                     dest_nlc=p.get("dest_nlc"))
    rep = _impact(state, change, frozenset({"splits"}))
    sp = rep.splits
    on = _name(state, change.corridor_origin_nlc)
    dn = _name(state, change.corridor_dest_nlc)
    ui: list[dict] = [_ui("toggleModule", {"module": "splits", "on": True}),
                      _ui("openTab", {"tab": "splits"})]
    if sp is None:
        return f"No split analysis available for {on} \u2192 {dn}.", ui
    opps = sorted((s for s in sp.post_change if s.status == "opportunity"),
                  key=lambda s: s.saving_pence, reverse=True)
    if not opps:
        return (f"No split-ticketing opportunities on {on} \u2192 {dn} "
                f"({sp.ticket_code}) after this change.", ui)
    lines = [f"{len(opps)} split-ticketing opportunit"
             f"{'y' if len(opps) == 1 else 'ies'} on {on} \u2192 {dn} "
             f"({sp.ticket_code}) after the change:"]
    for i, s in enumerate(opps[:3], 1):
        lines.append(f"{i}. Split at {_name(state, s.intermediate_nlc)}: "
                     f"through {_gbp(s.through_price_pence)}, legs "
                     f"{_gbp(s.leg1_price_pence)} + {_gbp(s.leg2_price_pence)} "
                     f"= {_gbp(s.split_total_pence)} \u2014 saves "
                     f"{_gbp(s.saving_pence)}.")
    if sp.notes:
        lines.append(sp.notes[0])
    ui.append(_ui("highlightStations",
                  {"nlcs": [opps[0].intermediate_nlc], "pulse": True}))
    return "\n".join(lines), ui


def _h_compare(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    keys = ("origin_nlc", "dest_nlc", "origin2_nlc", "dest2_nlc")
    if not all(p.get(k) for k in keys):
        return ("Compare needs two pairs \u2014 try \u201ccompare Manchester "
                "to Euston with Leeds to Kings Cross\u201d.", [])
    o, d, o2, d2 = (p[k] for k in keys)
    ticket = p.get("ticket_code", _DEFAULT_TICKET)
    r1, r2 = _resolve(state, o, d, ticket), _resolve(state, o2, d2, ticket)

    def side(r: ResolvedFare, a: str, b: str) -> str:
        an, bn = _name(state, a), _name(state, b)
        if r.status == "resolved":
            return f"{an} \u2192 {bn} = {_gbp(r.price_pence)}"
        return (f"{an} \u2192 {bn} = no {ticket} fare in the feed "
                f"({r.status.replace('_', ' ')})")

    parts = [f"{_ticket_desc(state, ticket)}: {side(r1, o, d)}; {side(r2, o2, d2)}."]
    if r1.price_pence is not None and r2.price_pence is not None:
        diff = r1.price_pence - r2.price_pence
        if diff == 0:
            parts.append("Same price.")
        else:
            cheap = (o, d) if diff > 0 else (o2, d2)
            parts.append(f"{_name(state, cheap[0])} \u2192 "
                         f"{_name(state, cheap[1])} is cheaper by "
                         f"{_gbp(abs(diff))}.")
    ui = [_ui("highlightStations", {"nlcs": [o, d, o2, d2], "pulse": True})]
    return " ".join(parts), ui


def _h_corridor(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    cid = p.get("corridor_id")
    c = _corridor_by_id(state, cid) if cid else _context_corridor(state, context)
    if c is None:
        known = ", ".join(k.get("id", "?") for k in state.vocab.corridors)
        return (f"I don't know the corridor \u201c{cid}\u201d. Loaded "
                f"corridors: {known or 'none'}.", [])
    sub = f" \u2014 {c['sub']}" if c.get("sub") else ""
    ui = [_ui("zoomToCorridor", {"corridorId": c.get("id")})]
    return f"Zooming to {c.get('name', c.get('id'))}{sub}.", ui


def _h_report(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    return ("Opening the impact report \u2014 every number in it comes from "
            "the deterministic engine.", [_ui("openReport", {})])


def _h_help(state: CopilotState, p: dict, context: dict) -> tuple[str, list]:
    return HELP_TEXT, []


_HANDLERS = {
    "resolve_fare": _h_resolve,
    "explain_provenance": _h_explain,
    "run_impact": _h_impact,
    "which_breach": _h_breach,
    "show_split": _h_split,
    "compare_fares": _h_compare,
    "show_corridor": _h_corridor,
    "open_report": _h_report,
    "help": _h_help,
}


# --- Entry point ------------------------------------------------------------


def answer(state: CopilotState, text: str, context: dict | None = None) -> dict:
    """English in → `{intent, confidence, answer_text, ui_commands}` out.

    Grammar first; LLM fallback only when the grammar misses. A clarify
    surface (unknown station, ambiguous name) is answered verbatim with no
    ui_commands — the copilot asks, it never guesses."""
    intent: Intent | None = parse(state.vocab, text)
    if intent is None:
        intent = llm_intent(state.vocab, text)
    if intent.clarify:
        return {"intent": intent.intent, "confidence": intent.confidence,
                "answer_text": intent.clarify, "ui_commands": []}
    handler = _HANDLERS.get(intent.intent, _h_help)
    try:
        answer_text, ui = handler(state, dict(intent.params), context or {})
    except Exception as exc:  # noqa: BLE001 — boundary: answer honestly, never 500
        log.exception("copilot dispatch failed for intent %s", intent.intent)
        answer_text, ui = f"The engine rejected that request: {exc}", []
    return {"intent": intent.intent, "confidence": intent.confidence,
            "answer_text": answer_text, "ui_commands": ui}
