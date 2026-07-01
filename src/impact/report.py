"""The ImpactReport dataclass + compute_impact public entry point.

`compute_impact(change, feed_paths) -> ImpactReport` is the single public
function the API surface / future LLM shell calls. Deterministic,
side-effect-free, surfaces honest gaps via `notes[]`.

The report is *modular*. Three blocks — compliance, anomalies, revenue —
are each Optional fields populated only when requested via the `include`
parameter. The always-present substrate (affected set, blast radius,
notes) is what every block plugs into. New modules (e.g. splits) follow
the same shape: an Optional block on the report, a key in `include`,
computed against the same substrate.

See `compliance.py`, `inversions.py`, `revenue.py`, `splits.py` for the
block-specific code; this file only orchestrates."""

from __future__ import annotations

from dataclasses import dataclass

from src.impact.affected import AffectedFare, AffectedSet, BlastRadiusPair, compute_affected_set
from src.impact.change_request import ChangeRequest, validate_against_feed
from src.impact.compliance import attach_compliance, build_corridor_regulation_map
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.impact.odm import ODMRevenueBlock, compute_odm_revenue, load_odm_index
from src.impact.revenue import per_flow_exposure, per_pair_exposure
from pathlib import Path
from typing import Callable

from src.impact.splits import SplitOpportunityResult, splits_for_change
from src.ingest.inspect import load_loc_meta
from src.perf import PerformanceResult, fetch_performance
from src.regulation import RegulationMap


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PERF_CACHE_DIR = REPO_ROOT / "data" / "perf_cache"
DEFAULT_PERF_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hsp"
# Default HSP window when the API caller didn't pin one: most recent 30 days
# (sliding); WEEKDAY day type matches the demo corridor's commuter use case.
DEFAULT_PERF_DAYS = "WEEKDAY"


# Known include keys. `compute_impact` raises ValueError on anything else
# so the API layer surfaces typos as 400s rather than silently dropping
# them. Adding a new module = add a key here + a block field + a branch.
KNOWN_INCLUDE_KEYS: frozenset[str] = frozenset({
    "compliance", "anomalies", "revenue", "revenue_odm", "splits", "performance",
})
DEFAULT_INCLUDE: frozenset[str] = frozenset({
    "compliance", "anomalies", "revenue",
})


@dataclass(frozen=True)
class ComplianceBlock:
    """The compliance analysis block (REGULATION.md §3 — the 0% freeze).

    Built by joining `AffectedSet.canonical` against the corridor's
    RegulationMap. When present, every row in `canonical_affected` carries
    a non-None `.compliance` verdict; when this block is absent (excluded
    via `include`), those `.compliance` fields are all None."""
    regulated_count: int               # canonical rows with status != 'not_regulated'
    breach_count: int                  # canonical rows with status == 'breach'
    breaches: tuple[AffectedFare, ...] # subset of canonical_affected flagged as breach
    regulation_map_notes: tuple[str, ...]  # the regmap's own disclosures


@dataclass(frozen=True)
class AnomaliesBlock:
    """Structural-anomaly detections over the affected set.

    Today: fare inversions only (return cheaper than single, etc.). The
    block exists so future anomaly kinds (inconsistent rounding, suspicious
    cluster spread) land here without changing the report's shape."""
    inversions: tuple[FareInversion, ...]


@dataclass(frozen=True)
class RevenueBlock:
    """Structural revenue exposure — labelled exposure, NOT a forecast.

    Two exposure numbers, named with their definitions embedded so the
    LLM shell (or any caller) cannot confuse them with revenue forecasts.
    """
    per_flow_exposure_pence: int       # static-demand sum across distinct repriced fares
    per_pair_exposure_pence: int       # cluster-weighted; GB-map view only, NOT revenue


@dataclass(frozen=True)
class PerformanceBlock:
    """Real-world punctuality overlay for the corridor (HSP serviceMetrics).

    Carries one `PerformanceResult` whose `mode` field is the freshness signal
    the UI must surface (live / cached / fixture) so callers cannot mistake
    stale data for live. The block is opt-in (`?include=performance`) because
    it touches outbound I/O; default callers never trigger a network call."""
    result: PerformanceResult


@dataclass(frozen=True)
class ImpactReport:
    """Aggregate output of `compute_impact`.

    Substrate (always present): change + affected set + blast radius + notes.
    Optional analysis blocks: each is None unless requested via `include`.
    The frontend / LLM shell renders whichever blocks are populated."""
    change: ChangeRequest
    canonical_affected: tuple[AffectedFare, ...]
    skipped: tuple[AffectedFare, ...]
    blast_radius_pairs: tuple[BlastRadiusPair, ...]
    notes: tuple[str, ...]             # assumptions + honest gaps
    # --- Optional analysis blocks ---------------------------------------
    compliance: ComplianceBlock | None = None
    anomalies: AnomaliesBlock | None = None
    revenue: RevenueBlock | None = None
    revenue_odm: ODMRevenueBlock | None = None
    splits: SplitOpportunityResult | None = None
    performance: PerformanceBlock | None = None


def _normalise_include(include: frozenset[str] | set[str] | None) -> frozenset[str]:
    if include is None:
        return DEFAULT_INCLUDE
    requested = frozenset(include)
    unknown = requested - KNOWN_INCLUDE_KEYS
    if unknown:
        raise ValueError(
            f"unknown include key(s): {sorted(unknown)}; "
            f"valid keys are {sorted(KNOWN_INCLUDE_KEYS)}"
        )
    return requested


def compute_impact(
    change: ChangeRequest,
    feed_paths: FeedPaths,
    *,
    include: frozenset[str] | set[str] | None = None,
    regulation_map: RegulationMap | None = None,
    performance_fetcher: Callable[..., PerformanceResult] | None = None,
) -> ImpactReport:
    """Orchestrate: validate → affected set → optional blocks → assemble.

    `include` selects which analysis blocks to compute. Default = all
    *core* blocks (compliance, anomalies, revenue). `splits` is opt-in
    because it's a corridor-wide re-resolution that costs extra and
    requires a chosen ticket. Unknown keys raise at the boundary.

    `regulation_map` is built per-corridor from the change if not supplied.
    Callers that want to inject a pre-built map (the LLM shell sharing one
    map across sibling changes) can pass it in."""
    requested = _normalise_include(include)

    validation = validate_against_feed(change, feed_paths)
    if not validation.ok:
        raise ValueError(
            "ChangeRequest failed feed validation: "
            + "; ".join(validation.errors)
        )

    notes: list[str] = list(validation.notes)
    notes.append(
        "synthetic discount applied without .RCM min-fare floor; a real "
        "railcard would carry an .RCM row pinning the floor per ticket. "
        "All canonical_affected new_price_pence values are unfloored."
    )

    affected_set: AffectedSet = compute_affected_set(change, feed_paths)
    notes.extend(affected_set.notes)

    compliance_block: ComplianceBlock | None = None
    if "compliance" in requested:
        regmap = regulation_map or build_corridor_regulation_map(change, feed_paths)
        affected_set = attach_compliance(
            affected_set, regmap,
            corridor_origin_nlc=change.corridor_origin_nlc,
            corridor_dest_nlc=change.corridor_dest_nlc,
        )
        notes.append(
            "is_london_flow inferred from a hardcoded London-terminals NLC set "
            "(src/impact/compliance.py:_LONDON_TERMINAL_NLCS); v2 should derive "
            "from .LOC FARE_GROUP."
        )
        breaches = tuple(
            f for f in affected_set.canonical
            if f.compliance is not None and f.compliance.status == "breach"
        )
        regulated = sum(
            1 for f in affected_set.canonical
            if f.compliance is not None and f.compliance.status != "not_regulated"
        )
        compliance_block = ComplianceBlock(
            regulated_count=regulated,
            breach_count=len(breaches),
            breaches=breaches,
            regulation_map_notes=regmap.notes,
        )

    anomalies_block: AnomaliesBlock | None = None
    if "anomalies" in requested:
        anomalies_block = AnomaliesBlock(
            inversions=detect_inversions(affected_set.canonical, feed_paths),
        )

    revenue_block: RevenueBlock | None = None
    if "revenue" in requested:
        revenue_block = RevenueBlock(
            per_flow_exposure_pence=per_flow_exposure(affected_set.canonical),
            per_pair_exposure_pence=per_pair_exposure(affected_set.canonical),
        )

    revenue_odm_block: ODMRevenueBlock | None = None
    if "revenue_odm" in requested:
        if feed_paths.odm_csv is None or not feed_paths.odm_csv.exists():
            notes.append(
                "revenue_odm block skipped: no ODM CSV at data/odm/odm.csv. "
                "Drop an ORR-style origin-destination matrix release there to "
                "populate this block; structural exposure remains available "
                "via the `revenue` block."
            )
        else:
            loc = load_loc_meta(feed_paths.loc)
            odm_index = load_odm_index(feed_paths.odm_csv, loc=loc)
            revenue_odm_block = compute_odm_revenue(affected_set, odm_index)

    splits_block: SplitOpportunityResult | None = None
    if "splits" in requested:
        splits_block = splits_for_change(change, feed_paths)
        notes.extend(splits_block.notes)

    performance_block: PerformanceBlock | None = None
    if "performance" in requested:
        from_crs, to_crs, perf_notes = _corridor_crses(change, feed_paths)
        notes.extend(perf_notes)
        if from_crs and to_crs:
            today = _today_iso()
            from_date = _iso_minus_days(today, 30)
            fetcher = performance_fetcher or _default_performance_fetcher
            perf = fetcher(
                from_crs, to_crs, from_date, today, DEFAULT_PERF_DAYS,
            )
            performance_block = PerformanceBlock(result=perf)
            notes.extend(perf.notes)
        else:
            notes.append(
                "performance block skipped: corridor endpoints have no CRS in .LOC "
                "(cluster NLCs with no member representative)."
            )

    return ImpactReport(
        change=change,
        canonical_affected=affected_set.canonical,
        skipped=affected_set.skipped,
        blast_radius_pairs=affected_set.blast_radius,
        notes=tuple(notes),
        compliance=compliance_block,
        anomalies=anomalies_block,
        revenue=revenue_block,
        revenue_odm=revenue_odm_block,
        splits=splits_block,
        performance=performance_block,
    )


def _corridor_crses(
    change: ChangeRequest, feed_paths: FeedPaths,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve corridor endpoints to CRS codes via .LOC.

    Cluster/group NLCs (e.g. London Terminals 1072) have no CRS of their own;
    we fall back to a representative member station, mirroring the
    splits._intermediates_from_timetable strategy."""
    loc = load_loc_meta(feed_paths.loc)
    notes: list[str] = []

    def _crs_for(nlc: str) -> str | None:
        meta = loc.get(nlc)
        if meta and meta.crs:
            return meta.crs
        # Cluster fallback: first member with a CRS.
        for member_nlc, m in loc.items():
            if m.group_nlc == nlc and m.crs:
                notes.append(
                    f"corridor endpoint {nlc} is a cluster; using member "
                    f"{member_nlc} ({m.crs}) as the CRS for HSP lookup."
                )
                return m.crs
        return None

    return (
        _crs_for(change.corridor_origin_nlc),
        _crs_for(change.corridor_dest_nlc),
        notes,
    )


def _default_performance_fetcher(
    from_crs: str, to_crs: str, from_date: str, to_date: str, days: str,
) -> PerformanceResult:
    return fetch_performance(
        from_crs, to_crs, from_date, to_date, days,  # type: ignore[arg-type]
        cache_dir=DEFAULT_PERF_CACHE_DIR,
        fixture_dir=DEFAULT_PERF_FIXTURE_DIR,
    )


def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


def _iso_minus_days(iso_today: str, n: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso_today.split("-"))
    return (date(y, m, d) - timedelta(days=n)).isoformat()


__all__ = [
    "AnomaliesBlock",
    "ComplianceBlock",
    "DEFAULT_INCLUDE",
    "ImpactReport",
    "KNOWN_INCLUDE_KEYS",
    "ODMRevenueBlock",
    "PerformanceBlock",
    "RevenueBlock",
    "compute_impact",
]
