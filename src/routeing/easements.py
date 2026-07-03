"""Easement matching — apply an .RGF EasementBundle to a JourneyQuery.

The .RGF file (RSPS5047 § 6.10) contains four record types per easement:

    E (header)   — dates, valid days, times, positive/negative, category
    L (location) — location scope with a modifier (origin/dest/via/exclude/…)
    D (detail)   — TOC / ticket / route / UID scope
    X (exception)— TOC / UID exceptions to the easement

Match semantics we implement (with the ambiguities called out in
comments where the spec is quiet, per CLAUDE.md's "never silently guess"
rule):

    header:  date in [start,end], day-of-week flag Y in valid_days,
             time in [start,end].  Empty valid_days / times => "always".

    L records:  Group by modifier.  Within a modifier: OR (any match
                satisfies the scope).  Across modifiers: AND.
                    - modifier 1 (applicable) — origin OR dest OR via
                    - modifier 2 (origin)     — origin equals
                    - modifier 3 (destination)— dest equals
                    - modifier 4 (via)        — journey passes through
                    - modifier 5 (exclude)    — journey does NOT contain
                    - modifier 6 (doubleback) — doubleback point
                We do NOT enforce modifier 4 (via) strictly when the caller
                did not supply a `via` list — spec is silent on default,
                so we treat "unknown" as "possibly matches" and note it.

    D records:  Group by detail_type. Within a type: OR (any match).
                Across types: AND (all types must match).

    X records:  If any exception applies, the easement is BLOCKED for
                this journey (even if the base conditions would have
                matched).

Returns an `EasementMatch` explaining exactly why the easement did or did
not fire; callers stitch these into the ValidityVerdict provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from typing import Literal

from src.ingest.routeing import EasementBundle


# 7-char valid_days mask starts at Monday per § 6.10.2.
_DOW_INDEX = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}

MatchOutcome = Literal["match", "no_match", "excepted", "outside_window"]


@dataclass(frozen=True)
class EasementMatch:
    """Verdict for one easement against one journey.

    `reasons` is the raw human-readable trace.  Callers use `outcome` for
    control flow; `reasons` for provenance display."""
    easement_ref: str
    outcome: MatchOutcome
    is_positive: bool          # easement_class == '1'
    reasons: tuple[str, ...]


def match_easement(
    bundle: EasementBundle,
    *,
    origin_crs: str,
    dest_crs: str,
    via_crs: tuple[str, ...] = (),
    ticket_code: str | None = None,
    route_code: str | None = None,
    toc: str | None = None,
    train_uid: str | None = None,
    query_date: _date | None = None,
    query_time_hhmm: str | None = None,
) -> EasementMatch:
    """Return whether `bundle` fires for the given journey."""
    reasons: list[str] = []
    hdr = bundle.header

    # --- Temporal window (§ 6.10.2) ---
    if not _date_in_window(query_date, hdr.start_date, hdr.end_date, reasons):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="outside_window",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )
    if not _day_matches(query_date, hdr.valid_days, reasons):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="outside_window",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )
    if not _time_in_window(query_time_hhmm, hdr.start_time, hdr.end_time, reasons):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="outside_window",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )

    # --- Location scope (§ 6.10.3, L records) ---
    if not _locations_match(
        bundle,
        origin_crs=origin_crs,
        dest_crs=dest_crs,
        via_crs=via_crs,
        reasons=reasons,
    ):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="no_match",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )

    # --- Detail scope (§ 6.10.4, D records) ---
    if not _details_match(
        bundle,
        ticket_code=ticket_code,
        route_code=route_code,
        toc=toc,
        train_uid=train_uid,
        reasons=reasons,
    ):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="no_match",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )

    # --- Exceptions (§ 6.10.5, X records) ---
    if _exceptions_apply(bundle, toc=toc, train_uid=train_uid, reasons=reasons):
        return EasementMatch(
            easement_ref=hdr.easement_ref,
            outcome="excepted",
            is_positive=hdr.easement_class == "1",
            reasons=tuple(reasons),
        )

    reasons.append(f"easement {hdr.easement_ref} FIRES ({'positive' if hdr.easement_class == '1' else 'negative'})")
    return EasementMatch(
        easement_ref=hdr.easement_ref,
        outcome="match",
        is_positive=hdr.easement_class == "1",
        reasons=tuple(reasons),
    )


# --- Temporal helpers ------------------------------------------------------


def _parse_ddmmyyyy(s: str) -> _date | None:
    """Return a date, or None if the field is empty / null.

    A `31122999` sentinel is interpreted as "no upper bound" — callers
    treat it as +inf, not as literal 2999."""
    if not s:
        return None
    if s == "31122999":
        return _date.max
    try:
        return datetime.strptime(s, "%d%m%Y").date()
    except ValueError:
        return None


def _date_in_window(
    query_date: _date | None,
    start: str,
    end: str,
    reasons: list[str],
) -> bool:
    if query_date is None:
        reasons.append("no query date supplied — skipping date-window check")
        return True
    s = _parse_ddmmyyyy(start)
    e = _parse_ddmmyyyy(end)
    if s is not None and query_date < s:
        reasons.append(f"query date {query_date} before easement start {s}")
        return False
    if e is not None and query_date > e:
        reasons.append(f"query date {query_date} after easement end {e}")
        return False
    return True


def _day_matches(
    query_date: _date | None, valid_days: str, reasons: list[str],
) -> bool:
    """Empty valid_days => "any day" per § 6.10.2 (field is optional)."""
    if not valid_days or not valid_days.strip():
        return True
    if query_date is None:
        return True
    dow = query_date.weekday()  # 0=Mon .. 6=Sun, matches the RSPS5047 order
    if dow >= len(valid_days):
        return True  # malformed valid_days — don't fabricate
    flag = valid_days[dow]
    if flag != "Y":
        reasons.append(
            f"query day {_DOW_INDEX[dow]} not in easement valid_days '{valid_days}'"
        )
        return False
    return True


def _time_in_window(
    query_time: str | None, start: str, end: str, reasons: list[str],
) -> bool:
    if not query_time:
        return True
    if not start and not end:
        return True
    if start and query_time < start:
        reasons.append(f"query time {query_time} before easement start {start}")
        return False
    if end and query_time > end:
        reasons.append(f"query time {query_time} after easement end {end}")
        return False
    return True


# --- Location scope --------------------------------------------------------


def _locations_match(
    bundle: EasementBundle,
    *,
    origin_crs: str,
    dest_crs: str,
    via_crs: tuple[str, ...],
    reasons: list[str],
) -> bool:
    """See module docstring for the modifier semantics."""
    if not bundle.locations:
        return True

    by_modifier: dict[str, list[str]] = {}
    for loc in bundle.locations:
        by_modifier.setdefault(loc.modifier, []).append(loc.location_crs)

    journey_locs = {origin_crs, dest_crs, *via_crs}

    for modifier, locs in by_modifier.items():
        if modifier == "1":  # applicable = origin OR dest OR via
            if not any(x in journey_locs for x in locs):
                reasons.append(
                    f"L(applicable) requires one of {locs} in journey; "
                    f"journey has {sorted(journey_locs)}"
                )
                return False
        elif modifier == "2":  # origin
            if origin_crs not in locs:
                reasons.append(f"L(origin) requires origin in {locs}; got {origin_crs}")
                return False
        elif modifier == "3":  # destination
            if dest_crs not in locs:
                reasons.append(f"L(dest) requires dest in {locs}; got {dest_crs}")
                return False
        elif modifier == "4":  # via
            if via_crs:
                if not any(v in via_crs for v in locs):
                    reasons.append(
                        f"L(via) requires one of {locs} in supplied via {list(via_crs)}"
                    )
                    return False
            else:
                # Spec is silent on the default when the caller didn't
                # supply a via list. We treat "unknown" as "possibly
                # matches" — flagged in provenance — so we don't silently
                # rule the easement out.
                reasons.append(
                    f"L(via) requires one of {locs}; no via supplied, assuming possible"
                )
        elif modifier == "5":  # exclude
            if any(x in journey_locs for x in locs):
                reasons.append(f"L(exclude) rejects journeys containing any of {locs}")
                return False
        elif modifier == "6":  # doubleback point
            # Modifier 6 marks the station a doubleback is allowed to.
            # There is always also a modifier=4 (via) record for the same
            # location for backwards compatibility (§ 6.10.3), which we
            # already handle above.  Nothing to do here.
            pass
    return True


# --- Detail scope ----------------------------------------------------------


def _details_match(
    bundle: EasementBundle,
    *,
    ticket_code: str | None,
    route_code: str | None,
    toc: str | None,
    train_uid: str | None,
    reasons: list[str],
) -> bool:
    if not bundle.details:
        return True
    # Group codes by detail_type.
    by_type: dict[str, list[str]] = {}
    for d in bundle.details:
        by_type.setdefault(d.detail_type, []).append(d.detail_code)

    checks: dict[str, str | None] = {
        "1": train_uid,     # UID
        "2": toc,           # TOC
        "3": route_code,    # ticket route
        "4": ticket_code,   # ticket code
    }

    for dtype, codes in by_type.items():
        supplied = checks.get(dtype)
        if supplied is None:
            # Caller didn't supply this scope — spec silent on default.
            # Treat as "possibly matches" and record in provenance.
            reasons.append(
                f"D(type={dtype}) requires one of {codes}; caller did not supply this"
            )
            continue
        if supplied not in codes:
            reasons.append(
                f"D(type={dtype}) requires one of {codes}; got {supplied!r}"
            )
            return False
    return True


# --- Exceptions ------------------------------------------------------------


def _exceptions_apply(
    bundle: EasementBundle,
    *,
    toc: str | None,
    train_uid: str | None,
    reasons: list[str],
) -> bool:
    if not bundle.exceptions:
        return False
    for x in bundle.exceptions:
        if x.exception_type == "1" and train_uid is not None and x.exception_code == train_uid:
            reasons.append(f"X(UID)={x.exception_code} excludes this journey")
            return True
        if x.exception_type == "2" and toc is not None and x.exception_code == toc:
            reasons.append(f"X(TOC)={x.exception_code} excludes this journey")
            return True
    return False


__all__ = ["EasementMatch", "MatchOutcome", "match_easement"]
