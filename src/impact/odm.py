"""Demand-weighted revenue block from an ORR-style ODM (Origin-Destination
Matrix) CSV. Labelled ESTIMATE — modelled from lagged demand data, NOT
settlement-grade.

Complements the existing structural `RevenueBlock` (`src/impact/revenue.py`):

  RevenueBlock              — static-demand exposure sums. No demand data.
  ODMRevenueBlock (here)    — same deltas weighted by real per-flow journey
                              volumes from an ODM release. Still an estimate:
                              the ODM is annual + lagged + product-agnostic,
                              and one journey ≠ one booking.

Wiring:
  - Opt-in via `?include=revenue_odm`.
  - Requires an ODM CSV at `data/odm/odm.csv` (auto-detected on FeedPaths).
    Absent CSV → block stays `None` and a note surfaces the fall-back.

Data model:
  Iterate `AffectedSet.canonical`. For each `AffectedFare`, iterate ALL of its
  `blast_radius_pairs` — the cluster fan-out. Each pair is looked up in the
  ODM by `(origin_nlc, dest_nlc)`, optionally further keyed by ticket_code
  when the ODM has ticket granularity. Unmatched pairs contribute zero and
  are counted separately so the UI can show gap coverage.

Column detection is deliberately lenient: real ORR releases have used
`origin_nlc`, `origin_station`, `originNLC`, `origin_crs`, etc. across
years. See `_infer_columns` for the substring rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.impact.affected import AffectedSet

if TYPE_CHECKING:
    from src.ingest.inspect import LocationMeta


# --- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class ODMRevenueEstimate:
    """Per-canonical-fare demand-weighted revenue delta.

    Not settlement-grade — modelled from lagged ODM. The `matched_pair_count`
    vs `unmatched_pair_count` split lets the UI show how much of the flow's
    blast radius the ODM actually covers."""
    flow_id: str
    ticket_code: str
    matched_pair_count: int
    unmatched_pair_count: int
    journeys_per_period: int          # summed over matched pairs
    delta_pence_per_journey: int      # new_price - old_price
    revenue_delta_pence: int          # journeys * delta


@dataclass(frozen=True)
class ODMRevenueBlock:
    """ESTIMATE — modelled from lagged ODM, not settlement-grade.

    Sums demand-weighted deltas across every canonical AffectedFare, iterating
    all blast-radius pairs. The `notes[0]` line is the caveat every consumer
    (UI, LLM shell) must surface verbatim."""
    estimates: tuple[ODMRevenueEstimate, ...]
    total_revenue_delta_pence: int
    matched_flow_count: int           # canonical rows with ≥1 matched pair
    unmatched_flow_count: int         # canonical rows with 0 matched pairs
    period_label: str                 # e.g. "ORR ODM 2023-24" (from filename)
    notes: tuple[str, ...]


# --- ODM index -----------------------------------------------------------


@dataclass(frozen=True)
class ODMIndex:
    """Lookup keyed by (origin_nlc, dest_nlc) → summed journeys/period.

    Ticket-aware ODMs (rare in the public releases) also populate
    `by_pair_and_ticket`; consumers try it first and fall back to `by_pair`
    with a note. Both maps are populated at load time.
    """
    by_pair: dict[tuple[str, str], int]
    by_pair_and_ticket: dict[tuple[str, str, str], int]
    period_label: str
    is_ticket_aware: bool
    row_count: int
    notes: tuple[str, ...]


# --- Loader --------------------------------------------------------------


def _infer_columns(columns: list[str]) -> dict[str, str | None]:
    """Map our logical fields to actual CSV headers, tolerant to naming drift.

    Returns dict with keys: origin, dest, journeys, ticket (optional).
    Raises ValueError with a specific complaint if origin/dest/journeys can't
    be located — better to fail loudly than to silently pick the wrong column."""
    lower = {c: c.lower() for c in columns}

    def _match(needles: tuple[str, ...]) -> str | None:
        for original, low in lower.items():
            for needle in needles:
                if needle in low:
                    return original
        return None

    origin = _match(("origin_nlc", "origin_station", "originnlc", "from_nlc", "from_station"))
    dest = _match(("dest_nlc", "destination_nlc", "dest_station", "destinationnlc",
                   "to_nlc", "to_station"))
    # Fall back to CRS if NLC columns are absent; caller re-maps via .LOC.
    origin_crs = _match(("origin_crs", "from_crs", "origincrs"))
    dest_crs = _match(("dest_crs", "destination_crs", "to_crs", "destcrs"))
    journeys = _match(("journey", "trips", "passenger", "demand", "volume"))
    ticket = _match(("ticket_code", "ticket_type", "product_code", "fare_code"))

    if journeys is None:
        raise ValueError(
            f"ODM CSV: could not identify a journey/demand column in {columns!r}; "
            "expected a header containing 'journey', 'trips', 'passenger', "
            "'demand' or 'volume'."
        )
    if origin is None and origin_crs is None:
        raise ValueError(
            f"ODM CSV: no origin column found in {columns!r}; "
            "expected 'origin_nlc' or 'origin_crs' (or a variant)."
        )
    if dest is None and dest_crs is None:
        raise ValueError(
            f"ODM CSV: no destination column found in {columns!r}; "
            "expected 'dest_nlc' or 'dest_crs' (or a variant)."
        )

    return {
        "origin": origin,
        "dest": dest,
        "origin_crs": origin_crs,
        "dest_crs": dest_crs,
        "journeys": journeys,
        "ticket": ticket,
    }


def _period_label_from_filename(path: Path) -> str:
    """Extract a human-readable period tag from the filename (best-effort)."""
    stem = path.stem
    return f"ODM ({stem})"


def load_odm_index(
    csv_path: Path,
    loc: dict[str, "LocationMeta"] | None = None,
) -> ODMIndex:
    """Load an ORR-style ODM CSV into a per-(o,d) journey-count lookup.

    Uses pandas because ORR releases are ~1.3M rows; a stdlib csv.DictReader
    would work but is 20-30× slower. Ticket-aware ODMs also populate the
    finer-grained map.

    When the CSV keys stations by CRS instead of NLC, `loc` is used to
    reverse-map CRS → NLC. Rows whose CRS is unknown are skipped and counted
    in the notes tuple."""
    # Import here so the module can be imported by callers that don't have
    # pandas installed — matches the timetable/perf pattern of degrading on
    # missing optional deps.
    import pandas as pd

    if not csv_path.exists():
        raise FileNotFoundError(f"ODM CSV not found: {csv_path}")

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    if len(df) == 0:
        return ODMIndex(
            by_pair={}, by_pair_and_ticket={},
            period_label=_period_label_from_filename(csv_path),
            is_ticket_aware=False, row_count=0,
            notes=("ODM CSV had zero data rows",),
        )

    cols = _infer_columns(list(df.columns))
    notes: list[str] = []

    # Build CRS→NLC map if needed.
    crs_to_nlc: dict[str, str] = {}
    if cols["origin"] is None or cols["dest"] is None:
        if loc is None:
            raise ValueError(
                "ODM CSV keys stations by CRS but no LOC index was passed; "
                "cannot reverse-map CRS to NLC."
            )
        for nlc, meta in loc.items():
            if meta.crs:
                # Multiple NLCs may share a CRS (e.g. cluster + member); we
                # keep the first-seen and note the collision on the caller's
                # ODM notes if it matters. Stable pick: sort by NLC.
                crs_to_nlc.setdefault(meta.crs.upper(), nlc)
        notes.append(
            f"ODM keyed by CRS; joined to NLC via .LOC (mapped {len(crs_to_nlc)} CRSes)."
        )

    def _origin_nlc(row: dict) -> str | None:
        if cols["origin"] is not None:
            v = str(row[cols["origin"]]).strip()
            return v or None
        crs = str(row[cols["origin_crs"]]).strip().upper()
        return crs_to_nlc.get(crs)

    def _dest_nlc(row: dict) -> str | None:
        if cols["dest"] is not None:
            v = str(row[cols["dest"]]).strip()
            return v or None
        crs = str(row[cols["dest_crs"]]).strip().upper()
        return crs_to_nlc.get(crs)

    is_ticket_aware = cols["ticket"] is not None
    by_pair: dict[tuple[str, str], int] = {}
    by_pair_and_ticket: dict[tuple[str, str, str], int] = {}
    dropped = 0

    for row in df.to_dict(orient="records"):
        o = _origin_nlc(row)
        d = _dest_nlc(row)
        if o is None or d is None:
            dropped += 1
            continue
        try:
            journeys = int(float(row[cols["journeys"]]))
        except (TypeError, ValueError):
            dropped += 1
            continue
        by_pair[(o, d)] = by_pair.get((o, d), 0) + journeys
        if is_ticket_aware:
            t = str(row[cols["ticket"]]).strip().upper()
            if t:
                by_pair_and_ticket[(o, d, t)] = (
                    by_pair_and_ticket.get((o, d, t), 0) + journeys
                )

    if dropped:
        notes.append(
            f"skipped {dropped} rows with unresolvable origin/dest or non-numeric "
            "demand — no silent guesses."
        )

    return ODMIndex(
        by_pair=by_pair,
        by_pair_and_ticket=by_pair_and_ticket,
        period_label=_period_label_from_filename(csv_path),
        is_ticket_aware=is_ticket_aware,
        row_count=len(df),
        notes=tuple(notes),
    )


# --- Compute -------------------------------------------------------------


def compute_odm_revenue(
    affected_set: AffectedSet,
    odm: ODMIndex,
) -> ODMRevenueBlock:
    """Sum demand-weighted deltas across every canonical AffectedFare.

    For each fare: iterate its blast_radius_pairs, look up journeys per pair
    in the ODM. If the ODM is ticket-aware, prefer the (o,d,ticket) match;
    fall back to (o,d) aggregate with a note.

    Deltas come straight from `new_price_pence - old_price_pence`, so the
    sign convention (discounts → negative) matches `revenue.py`."""
    estimates: list[ODMRevenueEstimate] = []
    matched_flow_count = 0
    unmatched_flow_count = 0
    ticket_aggregation_used = False

    for fare in affected_set.canonical:
        if fare.old_price_pence is None or fare.new_price_pence is None:
            # Non-resolved rows carry no delta; skip cleanly.
            continue
        delta = fare.new_price_pence - fare.old_price_pence

        journeys = 0
        matched = 0
        unmatched = 0
        for (o, d) in fare.blast_radius_pairs:
            pair_journeys: int | None = None
            if odm.is_ticket_aware:
                pair_journeys = odm.by_pair_and_ticket.get((o, d, fare.ticket_code))
                if pair_journeys is None and (o, d) in odm.by_pair:
                    # Fall back to ticket-aggregated demand.
                    pair_journeys = odm.by_pair[(o, d)]
                    ticket_aggregation_used = True
            else:
                pair_journeys = odm.by_pair.get((o, d))

            if pair_journeys is None:
                unmatched += 1
            else:
                journeys += pair_journeys
                matched += 1

        if matched > 0:
            matched_flow_count += 1
        else:
            unmatched_flow_count += 1

        estimates.append(ODMRevenueEstimate(
            flow_id=fare.flow_id,
            ticket_code=fare.ticket_code,
            matched_pair_count=matched,
            unmatched_pair_count=unmatched,
            journeys_per_period=journeys,
            delta_pence_per_journey=delta,
            revenue_delta_pence=journeys * delta,
        ))

    total = sum(e.revenue_delta_pence for e in estimates)

    notes: list[str] = [
        "ESTIMATE — demand-weighted revenue delta modelled from lagged ODM. "
        "One ODM journey ≠ one booking; the ODM is annual + product-agnostic; "
        "figures are NOT settlement-grade and MUST NOT be quoted as revenue.",
    ]
    if ticket_aggregation_used:
        notes.append(
            "some flows fell back to ticket-aggregated demand (ODM had no row "
            "for the specific ticket_code)."
        )
    if unmatched_flow_count:
        notes.append(
            f"{unmatched_flow_count}/{len(estimates)} canonical flows had zero "
            "ODM coverage across their blast radius; those contributed 0 to the "
            "total (fall back to structural exposure to see them)."
        )
    notes.extend(odm.notes)

    return ODMRevenueBlock(
        estimates=tuple(estimates),
        total_revenue_delta_pence=total,
        matched_flow_count=matched_flow_count,
        unmatched_flow_count=unmatched_flow_count,
        period_label=odm.period_label,
        notes=tuple(notes),
    )


__all__ = [
    "ODMIndex",
    "ODMRevenueBlock",
    "ODMRevenueEstimate",
    "compute_odm_revenue",
    "load_odm_index",
]
