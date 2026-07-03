"""Deterministic grammar for the copilot's canonical queries.

Grammar runs FIRST; the LLM is a fallback for phrasing the patterns miss.
Every entity (station, corridor, railcard) resolves against a Vocabulary
built from the same data the API serves. Ambiguity is surfaced as an honest
clarification (`Intent.clarify`) — never guessed.

Canonical queries covered:
  "fare from X to Y [with a Z railcard]"   → resolve_fare
  "why is it that price" / "explain …"     → explain_provenance
  "show the splits"                        → show_split
  "zoom to the corridor" / "show man-eus"  → show_corridor
  "run the impact"                         → run_impact
  "what does this change cost"             → run_impact (revenue-led answer)
  "open the report"                        → open_report
  "which fares breach"                     → which_breach
  "compare X and Y"                        → compare_fares
  "help"                                   → help
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.copilot.intents import Intent

# Common RSPS5045 ticket codes accepted as an explicit override in queries.
_TICKET_CODES = {"SOR", "SOS", "SVR", "SDS", "SDR", "CDR", "CDS", "FOS", "FOR", "SSS"}


@dataclass(frozen=True)
class Vocabulary:
    """Entity lookup tables. Built once from API state (see build_vocabulary);
    tests construct small synthetic ones — no feed required."""
    corridors: tuple[dict, ...] = ()
    # normalized station name → (nlc, display). Only names with a fares NLC.
    stations: dict[str, tuple[str, str]] = field(default_factory=dict)
    crs_to_nlc: dict[str, str] = field(default_factory=dict)
    railcards: dict[str, str] = field(default_factory=dict)  # normalized name → code


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 &-]", " ", text.lower()).strip()


def _corridor_sides(c: dict) -> tuple[str, str]:
    """Normalized (origin, dest) name halves of 'Manchester – London Euston'."""
    name = c.get("name", "")
    parts = re.split(r"\s*[–—-]\s*", name, maxsplit=1)
    if len(parts) == 2:
        return normalize(parts[0]), normalize(parts[1])
    return normalize(name), ""


def _endpoint_aliases(vocab: Vocabulary) -> dict[str, list[tuple[str, str]]]:
    """alias → [(nlc, display)] from curated corridor endpoints. An alias
    like 'london' maps to several termini; the pair snapper disambiguates."""
    out: dict[str, list[tuple[str, str]]] = {}

    def add(alias: str, nlc: str, display: str) -> None:
        if not alias or not nlc:
            return
        hits = out.setdefault(alias, [])
        if all(h[0] != nlc for h in hits):
            hits.append((nlc, display))

    for c in vocab.corridors:
        o_name, d_name = _corridor_sides(c)
        add(o_name, c.get("origin_nlc", ""), o_name.title())
        add(d_name, c.get("dest_nlc", ""), d_name.title())
        # Single significant words too ("euston", "leeds"); "london" stays
        # ambiguous across termini on purpose — the snapper resolves it.
        for side, nlc in ((o_name, c.get("origin_nlc", "")),
                          (d_name, c.get("dest_nlc", ""))):
            for w in side.split():
                if len(w) >= 4:
                    add(w, nlc, side.title())
        add(normalize(c.get("origin_crs", "")), c.get("origin_nlc", ""), o_name.title())
        add(normalize(c.get("dest_crs", "")), c.get("dest_nlc", ""), d_name.title())
    return out


def resolve_place(vocab: Vocabulary, token: str) -> tuple[str, list[tuple[str, str]]]:
    """('ok'|'ambiguous'|'miss', candidates). Order of authority:
    raw NLC → CRS → corridor-endpoint alias → exact station name →
    unique station-name prefix. Multiple hits are returned, not guessed."""
    t = normalize(token)
    if not t:
        return "miss", []
    if re.fullmatch(r"[0-9][0-9a-z]{3}", t):
        return "ok", [(t.upper() if t.isalnum() else t, token.upper())]
    if re.fullmatch(r"[a-z]{3}", t) and t.upper() in vocab.crs_to_nlc:
        return "ok", [(vocab.crs_to_nlc[t.upper()], token.upper())]
    aliases = _endpoint_aliases(vocab)
    if t in aliases:
        hits = aliases[t]
        return ("ok" if len(hits) == 1 else "ambiguous"), hits
    if t in vocab.stations:
        nlc, disp = vocab.stations[t]
        return "ok", [(nlc, disp)]
    pref = [(nlc, disp) for name, (nlc, disp) in vocab.stations.items()
            if name.startswith(t)]
    # Distinct NLCs only — 'manchester' hitting five Manchester stations is
    # a real ambiguity; the same station twice is not.
    seen: dict[str, str] = {}
    for nlc, disp in pref:
        seen.setdefault(nlc, disp)
    hits = [(nlc, disp) for nlc, disp in seen.items()]
    if len(hits) == 1:
        return "ok", hits
    if len(hits) > 1:
        return "ambiguous", hits[:4]
    return "miss", []


def find_pair(vocab: Vocabulary, o_text: str, d_text: str
              ) -> tuple[str, str, str, str, str | None] | str:
    """Resolve an O/D pair → (o_nlc, o_disp, d_nlc, d_disp, corridor_id|None),
    or a clarification string. A curated corridor pairing snaps ambiguous
    sides ('manchester to london' → man-eus) before anything else."""
    so, co = resolve_place(vocab, o_text)
    sd, cd = resolve_place(vocab, d_text)
    if so == "miss":
        return f"I don't know a station called \u201c{o_text.strip()}\u201d."
    if sd == "miss":
        return f"I don't know a station called \u201c{d_text.strip()}\u201d."
    for c in vocab.corridors:
        o_nlc, d_nlc = c.get("origin_nlc", ""), c.get("dest_nlc", "")
        cid = c.get("id", "") or None
        for a, b in ((o_nlc, d_nlc), (d_nlc, o_nlc)):
            if any(h[0] == a for h in co) and any(h[0] == b for h in cd):
                o_disp = next(h[1] for h in co if h[0] == a)
                d_disp = next(h[1] for h in cd if h[0] == b)
                return a, o_disp, b, d_disp, cid
    if so == "ok" and sd == "ok":
        return co[0][0], co[0][1], cd[0][0], cd[0][1], None
    amb_text, amb = (o_text, co) if so == "ambiguous" else (d_text, cd)
    names = " / ".join(d for _, d in amb)
    return (f"\u201c{amb_text.strip()}\u201d could be {names} — "
            "which did you mean?")


def find_corridor(vocab: Vocabulary, text: str) -> dict | None:
    t = normalize(text)
    for c in vocab.corridors:
        if c.get("id", "").lower() in t.split():
            return c
    for c in vocab.corridors:
        o_name, d_name = _corridor_sides(c)
        if o_name and d_name and o_name in t and d_name in t:
            return c
    for c in vocab.corridors:
        o_name, _ = _corridor_sides(c)
        if o_name and o_name in t:
            return c
    return None


def _ticket_code(text: str) -> str | None:
    for tok in re.findall(r"\b[A-Za-z]{3}\b", text):
        if tok.upper() in _TICKET_CODES:
            return tok.upper()
    return None


def _railcard(vocab: Vocabulary, text: str) -> tuple[str | None, str | None]:
    """(railcard_code, clarify). Only fires when 'railcard' is mentioned."""
    t = normalize(text)
    if "railcard" not in t:
        return None, None
    for name, code in vocab.railcards.items():
        if name and name in t:
            return code, None
    core = t.split("railcard")[0]
    m = re.search(r"with (?:an? )?([a-z0-9 &-]+?)\s*$", core)
    asked = m.group(1).strip() if m else ""
    known = ", ".join(sorted(vocab.railcards)) or "none loaded"
    return None, (f"I don't recognise the railcard \u201c{asked or 'requested'}\u201d. "
                  f"Feed railcards I know: {known}.")


_PAIR = r"(?:from\s+)?(.+?)\s+(?:to|and)\s+(.+?)"


def parse(vocab: Vocabulary, text: str) -> Intent | None:
    """Deterministic parse. None → caller may try the LLM fallback."""
    raw = text.strip()
    t = normalize(raw)
    if not t:
        return Intent("help", confidence=1.0)

    if re.fullmatch(r"(help|h|\?|hi|hello|what can you do( here)?|"
                    r"what do you do)", t):
        return Intent("help", confidence=1.0)

    # -- compare X and Y ----------------------------------------------------
    m = re.match(r"compare\s+(.+?)\s+(?:with|vs\.?|versus|against|and)\s+(.+)$", t)
    if m:
        left, right = m.group(1), m.group(2)
        sides: list[tuple[str, str]] = []
        clar = None
        for part in (left, right):
            pm = re.match(_PAIR + r"$", part)
            got = find_pair(vocab, pm.group(1), pm.group(2)) if pm and " to " in part else None
            if got is None:
                c = find_corridor(vocab, part)
                if c:
                    got = (c["origin_nlc"], _corridor_sides(c)[0].title(),
                           c["dest_nlc"], _corridor_sides(c)[1].title(), c["id"])
                else:
                    clar = f"I couldn't match \u201c{part.strip()}\u201d to a corridor or station pair."
                    break
            if isinstance(got, str):
                clar = got
                break
            sides.append((got[0], got[2]))
        if clar:
            return Intent("compare_fares", confidence=0.6, clarify=clar)
        params = {"origin_nlc": sides[0][0], "dest_nlc": sides[0][1],
                  "origin2_nlc": sides[1][0], "dest2_nlc": sides[1][1]}
        tc = _ticket_code(raw)
        if tc:
            params["ticket_code"] = tc
        return Intent("compare_fares", params, 0.95)

    # -- why / explain provenance -------------------------------------------
    if re.search(r"\bwhy\b", t) or t.startswith("explain"):
        params: dict = {}
        m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+cost| priced|\?|$)", t)
        if m:
            got = find_pair(vocab, m.group(1), m.group(2))
            if isinstance(got, str):
                return Intent("explain_provenance", confidence=0.6, clarify=got)
            params = {"origin_nlc": got[0], "dest_nlc": got[2]}
        tc = _ticket_code(raw)
        if tc:
            params["ticket_code"] = tc
        return Intent("explain_provenance", params, 0.9)

    # -- what does this change cost → impact, revenue-led --------------------
    if re.search(r"\b(change|proposal|it)\b.*\bcost\b", t) or \
       re.search(r"\bcost\b.*\b(change|proposal)\b", t) or \
       "revenue" in t or "exposure" in t:
        return Intent("run_impact", confidence=0.9)

    # -- run the impact -------------------------------------------------------
    if "impact" in t and re.search(r"\b(run|compute|recompute|re-run|rerun|analy[sz]e|show)\b", t):
        return Intent("run_impact", confidence=0.95)

    # -- splits ---------------------------------------------------------------
    if "split" in t:
        params = {}
        m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\?|$)", t)
        if m:
            got = find_pair(vocab, m.group(1), m.group(2))
            if isinstance(got, str):
                return Intent("show_split", confidence=0.6, clarify=got)
            params = {"origin_nlc": got[0], "dest_nlc": got[2]}
        return Intent("show_split", params, 0.95)

    # -- report ---------------------------------------------------------------
    if "report" in t and re.search(r"\b(open|show|generate|see|view|the)\b", t):
        return Intent("open_report", confidence=0.95)

    # -- breaches -------------------------------------------------------------
    if "breach" in t or "breaches" in t or \
       re.search(r"\b(over|above|exceed)\w*\b.*\bcap\b", t) or "non-compliant" in t:
        return Intent("which_breach", confidence=0.95)

    # -- fare from X to Y ------------------------------------------------------
    m = re.search(r"(?:fare|price|how much|cost)\b.*?\bfrom\s+(.+?)\s+to\s+"
                  r"(.+?)(?:\s+with\b(.*))?(?:\?|$)", t) or \
        re.match(r"fare\s+(.+?)\s+(?:to|-)\s+(.+?)(?:\s+with\b(.*))?(?:\?|$)", t)
    if m:
        got = find_pair(vocab, m.group(1), m.group(2))
        if isinstance(got, str):
            return Intent("resolve_fare", confidence=0.6, clarify=got)
        params = {"origin_nlc": got[0], "dest_nlc": got[2]}
        tc = _ticket_code(raw)
        if tc:
            params["ticket_code"] = tc
        rc, rc_clar = _railcard(vocab, raw)
        if rc_clar:
            return Intent("resolve_fare", params, 0.6, clarify=rc_clar)
        if rc:
            params["railcard_code"] = rc
        return Intent("resolve_fare", params, 0.95)

    # -- zoom / show corridor --------------------------------------------------
    if "zoom" in t or "corridor" in t or t.startswith("show ") or t.startswith("go to "):
        c = find_corridor(vocab, t)
        if c:
            return Intent("show_corridor", {"corridor_id": c["id"]}, 0.95)
        if "corridor" in t or "zoom" in t:
            return Intent("show_corridor", {}, 0.85)  # context corridor

    return None


def build_vocabulary(corridors: list[dict],
                     station_names: dict[str, tuple[str, str]],
                     crs_to_nlc: dict[str, str],
                     railcards: dict[str, str]) -> Vocabulary:
    return Vocabulary(
        corridors=tuple(corridors),
        stations={normalize(k): v for k, v in station_names.items()},
        crs_to_nlc=crs_to_nlc,
        railcards={normalize(k): v for k, v in railcards.items()},
    )
