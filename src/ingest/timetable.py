"""Minimal CIF (Common Interface File) parser for the RSPS5046 timetable
feed.

Tightly scoped: TIPLOC index + per-train passenger calling sequence. Used
by the split-ticket module to verify NRCoT Cond. 14 — "does a train on
this corridor actually call at the proposed split point" — replacing the
hardcoded WCML whitelist with real call-pattern data.

What this parser does NOT do (deferred):
  - Associations (AA): joining/dividing portions.
  - STP overlay/cancel merging: we use Permanent (P) + STP New (N) only.
    Overlays and cancels are recorded in `quarantined` for visibility.
  - Change-en-route (CR) handling.
  - Days-run / bank-holiday filtering — we answer "ever calls at" not
    "calls at on date X."

CIF format: 80-char fixed-width records, latin-1 encoded, first 2 chars
identify the record type. Reference: Network Rail CIF End-User Spec v29,
RSPS5046 spec. Offsets cited inline against the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.ingest.inspect import _cached


# --- Record offsets (RSPS5046 / Network Rail CIF spec) -------------------

# TI: TIPLOC Insert (record type 'TI'). 80 chars.
#   0-1   record type 'TI'
#   2-8   TIPLOC code (7 chars)
#   9-10  capitals indicator
#   11-16 NALCO
#   17    checksum
#   18-43 TPS description (26 chars)
#   44-48 STANOX
#   49-52 PO MCP code
#   53-55 CRS code (3 chars; spaces if none)
#   56-71 description (16 chars, NLC description)

# BS: Basic Schedule. 80 chars.
#   0-1   'BS'
#   2     transaction type (N=new, D=delete, R=revise)
#   3-8   train UID (6 chars)
#   9-14  date runs from (YYMMDD)
#   15-20 date runs to (YYMMDD)
#   21-27 days run (7-char bit mask Mon..Sun)
#   28    bank holiday running
#   29    train status (P=Passenger STP, 1=STP passenger, B=bus, F=freight,...)
#   30-31 train category (e.g. OO=ordinary passenger, XX=express passenger)
#   32-35 train identity (signalling headcode)
#   ...
#   77    STP indicator (P=permanent, N=STP new, O=overlay, C=cancellation)

# LO: Origin location. 80 chars.
#   0-1   'LO'
#   2-9   location (TIPLOC + suffix)  ← we use just first 7 chars stripped
#   10-14 scheduled departure (WTT, HHMM[H])
#   15-19 public departure (HHMM, '0000' = non-public)
#   20-22 platform
#   ...

# LI: Intermediate location. 80 chars.
#   0-1   'LI'
#   2-9   location
#   10-14 scheduled arrival (WTT)
#   15-19 scheduled departure (WTT)
#   20-24 pass (WTT, set when train passes without stopping)
#   25-28 public arrival (HHMM, '0000' if pass)
#   29-32 public departure (HHMM, '0000' if pass)
#   33-35 platform
#   ...
#   42-53 activity codes (12 chars, 2-char tokens — 'T'=stops to take up
#         and set down, 'D'=set down only, 'U'=take up only, 'R'=request)

# LT: Terminating location. 80 chars.
#   0-1   'LT'
#   2-9   location
#   10-14 scheduled arrival (WTT)
#   15-18 public arrival (HHMM)
#   ...


# Train statuses we treat as passenger services. The CIF spec lists many;
# we take the two relevant ones for a current passenger snapshot.
_PASSENGER_TRAIN_STATUSES: frozenset[str] = frozenset({"P", "1"})

# STP indicators we accept into the index. 'P' permanent and 'N' STP-new
# are the running schedules; 'O' overlays and 'C' cancellations are dropped
# (proper merging needs association handling).
_ACCEPTED_STP: frozenset[str] = frozenset({"P", "N"})


@dataclass(frozen=True)
class TipLocMeta:
    """One TIPLOC row from a TI record."""
    tiploc: str           # 7-char CIF location identifier
    crs: str | None       # 3-char CRS booking code, or None if blank
    description: str      # readable name (TPS description, stripped)


@dataclass(frozen=True)
class CallingPoint:
    """One stop on a train's calling sequence (already filtered to public
    calls — pass-through LI records are excluded at build time)."""
    tiploc: str
    crs: str | None       # joined from TipLocMeta; None if TIPLOC unknown
    is_origin: bool
    is_terminus: bool


@dataclass(frozen=True)
class TrainSchedule:
    """One BS-rooted passenger schedule with its public calling sequence.

    Calling points appear in service order. `train_uid` + `stp_indicator`
    identifies the schedule version uniquely under v1 (no overlay merge).
    """
    train_uid: str
    stp_indicator: str
    train_status: str
    train_category: str
    calling_points: tuple[CallingPoint, ...]


@dataclass(frozen=True)
class TimetableIndex:
    """Aggregate output of `load_timetable_index`. Frozen so it can sit in
    the module cache without aliasing surprises."""
    tiplocs: dict[str, TipLocMeta]
    crs_to_tiplocs: dict[str, tuple[str, ...]]
    schedules: tuple[TrainSchedule, ...]
    quarantined: tuple[str, ...]
    notes: tuple[str, ...]
    source_file: str       # for provenance in downstream notes


def load_timetable_index(mca_path: Path) -> TimetableIndex:
    """Parse a CIF .MCA file into the in-memory index. Cached on
    (path, mtime, size) — re-reading a multi-MB file per query would
    dominate latency."""
    return _cached(Path(mca_path), _build_timetable_index)


def _build_timetable_index(mca_path: Path) -> TimetableIndex:
    tiplocs: dict[str, TipLocMeta] = {}
    schedules: list[TrainSchedule] = []
    quarantined: list[str] = []

    # Single-pass parse. The CIF format guarantees BS records precede their
    # LO/LI/LT records in the file (per train), so we accumulate calling
    # points into the current schedule and flush on the next BS / file end.
    current_bs: dict[str, str] | None = None
    current_calls: list[CallingPoint] = []
    in_passenger_train = False  # True iff current BS passed our filter

    def flush_current() -> None:
        nonlocal current_bs, current_calls, in_passenger_train
        if current_bs is not None and in_passenger_train and current_calls:
            schedules.append(TrainSchedule(
                train_uid=current_bs["train_uid"],
                stp_indicator=current_bs["stp_indicator"],
                train_status=current_bs["train_status"],
                train_category=current_bs["train_category"],
                calling_points=tuple(current_calls),
            ))
        current_bs = None
        current_calls = []
        in_passenger_train = False

    with mca_path.open("r", encoding="latin-1") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if len(line) < 2:
                continue
            rec = line[:2]

            if rec == "TI":
                if len(line) < 56:
                    quarantined.append(f"line {line_no}: TI truncated ({len(line)} chars)")
                    continue
                tiploc = line[2:9].strip()
                if not tiploc:
                    quarantined.append(f"line {line_no}: TI empty TIPLOC")
                    continue
                crs_raw = line[53:56].strip()
                desc = line[18:44].strip()
                tiplocs[tiploc] = TipLocMeta(
                    tiploc=tiploc,
                    crs=crs_raw or None,
                    description=desc,
                )

            elif rec == "BS":
                # New train; flush the previous one first.
                flush_current()
                if len(line) < 79:
                    quarantined.append(f"line {line_no}: BS truncated ({len(line)} chars)")
                    continue
                train_uid = line[3:9].strip()
                train_status = line[29:30]
                train_category = line[30:32]
                stp = line[79] if len(line) > 79 else line[78] if len(line) > 78 else " "
                if (
                    train_status in _PASSENGER_TRAIN_STATUSES
                    and stp in _ACCEPTED_STP
                ):
                    current_bs = {
                        "train_uid": train_uid,
                        "stp_indicator": stp,
                        "train_status": train_status,
                        "train_category": train_category,
                    }
                    in_passenger_train = True
                else:
                    # Non-passenger or overlay/cancel — record reason once
                    # per BS so the count is auditable but not noisy.
                    if train_status not in _PASSENGER_TRAIN_STATUSES:
                        quarantined.append(
                            f"line {line_no}: BS {train_uid} status={train_status!r} non-passenger; dropped"
                        )
                    elif stp not in _ACCEPTED_STP:
                        quarantined.append(
                            f"line {line_no}: BS {train_uid} stp={stp!r} not in P/N; overlay merging deferred"
                        )

            elif rec == "LO" and in_passenger_train:
                if len(line) < 10:
                    quarantined.append(f"line {line_no}: LO truncated")
                    continue
                tiploc = line[2:9].strip()
                if not tiploc:
                    continue
                meta = tiplocs.get(tiploc)
                current_calls.append(CallingPoint(
                    tiploc=tiploc,
                    crs=meta.crs if meta else None,
                    is_origin=True,
                    is_terminus=False,
                ))

            elif rec == "LI" and in_passenger_train:
                if len(line) < 33:
                    quarantined.append(f"line {line_no}: LI truncated")
                    continue
                tiploc = line[2:9].strip()
                if not tiploc:
                    continue
                public_arr = line[25:29]
                public_dep = line[29:33]
                # Filter: only retain stops where the train is publicly
                # advertised to call. '0000' / spaces = pass-through.
                if public_arr.strip() in ("", "0000") and public_dep.strip() in ("", "0000"):
                    continue
                meta = tiplocs.get(tiploc)
                current_calls.append(CallingPoint(
                    tiploc=tiploc,
                    crs=meta.crs if meta else None,
                    is_origin=False,
                    is_terminus=False,
                ))

            elif rec == "LT" and in_passenger_train:
                if len(line) < 10:
                    quarantined.append(f"line {line_no}: LT truncated")
                    continue
                tiploc = line[2:9].strip()
                if not tiploc:
                    continue
                meta = tiplocs.get(tiploc)
                current_calls.append(CallingPoint(
                    tiploc=tiploc,
                    crs=meta.crs if meta else None,
                    is_origin=False,
                    is_terminus=True,
                ))

            # All other record types (HD, BX, CR, AA, ZZ, TN, LN, TD, TA)
            # silently skipped in v1.

        flush_current()

    # Build CRS reverse map. A CRS can in rare cases map to several
    # TIPLOCs (e.g. amalgamated station codes); preserve all of them.
    crs_to_tiplocs: dict[str, list[str]] = {}
    for tiploc, meta in tiplocs.items():
        if meta.crs:
            crs_to_tiplocs.setdefault(meta.crs, []).append(tiploc)

    notes = (
        "STP overlays (O) and cancellations (C) are NOT merged in v1; the "
        "schedule list is the permanent (P) and STP-new (N) running pattern. "
        "Day-of-week and bank-holiday masks are not applied — \"calls at\" "
        "means \"ever calls at on this snapshot\", not \"calls at on date X\". "
        "Associations (AA) — joining/dividing services — are not handled.",
    )

    return TimetableIndex(
        tiplocs=tiplocs,
        crs_to_tiplocs={k: tuple(v) for k, v in crs_to_tiplocs.items()},
        schedules=tuple(schedules),
        quarantined=tuple(quarantined),
        notes=notes,
        source_file=mca_path.name,
    )


# --- Corridor helpers -----------------------------------------------------


def trains_serving_corridor(
    idx: TimetableIndex,
    origin_crs: str,
    dest_crs: str,
) -> tuple[TrainSchedule, ...]:
    """Return schedules whose calling sequence contains `origin_crs` then
    `dest_crs` in service order (subsequence — covers direct and indirect
    services). The reverse-direction trains for this corridor are excluded;
    callers wanting both directions should call this twice and union.

    Empty result is meaningful: no service in the snapshot links the two
    stations. We never fabricate a service; callers should fall back to
    the deferred-validity note in that case.
    """
    matches: list[TrainSchedule] = []
    for s in idx.schedules:
        seq = [cp.crs for cp in s.calling_points]
        try:
            i = seq.index(origin_crs)
        except ValueError:
            continue
        if dest_crs in seq[i + 1:]:
            matches.append(s)
    return tuple(matches)


def intermediate_calls(
    idx: TimetableIndex,
    origin_crs: str,
    dest_crs: str,
) -> tuple[str, ...]:
    """Union of CRS codes called at strictly between `origin_crs` and
    `dest_crs` across all corridor-serving trains. Sorted, deduplicated.
    Endpoints excluded.

    This is the input the splits module needs: real call points to consider
    as split candidates, instead of a hardcoded whitelist.
    """
    intermediates: set[str] = set()
    for s in trains_serving_corridor(idx, origin_crs, dest_crs):
        seq = [cp.crs for cp in s.calling_points]
        try:
            i = seq.index(origin_crs)
            j = seq.index(dest_crs, i + 1)
        except ValueError:
            continue
        for crs in seq[i + 1:j]:
            if crs and crs != origin_crs and crs != dest_crs:
                intermediates.add(crs)
    return tuple(sorted(intermediates))


__all__ = [
    "CallingPoint",
    "TimetableIndex",
    "TipLocMeta",
    "TrainSchedule",
    "intermediate_calls",
    "load_timetable_index",
    "trains_serving_corridor",
]
