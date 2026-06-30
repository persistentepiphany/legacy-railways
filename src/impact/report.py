"""The ImpactReport dataclass + compute_impact public entry point.

`compute_impact(change, feed_paths) -> ImpactReport` is the single public
function the API surface / future LLM shell calls. Deterministic,
side-effect-free, surfaces honest gaps via `notes[]`.

The report now carries compliance: every canonical row gets a verdict
(compliant / breach / not_regulated) joined from the corridor's regulation
map. See src/impact/compliance.py for the join and REGULATION.md §3 for
the rule being enforced (the 0% freeze)."""

from __future__ import annotations

from dataclasses import dataclass

from src.impact.affected import AffectedFare, BlastRadiusPair, compute_affected_set
from src.impact.change_request import ChangeRequest, validate_against_feed
from src.impact.compliance import attach_compliance, build_corridor_regulation_map
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.impact.revenue import per_flow_exposure, per_pair_exposure
from src.regulation import RegulationMap


@dataclass(frozen=True)
class ImpactReport:
    """Aggregate output of `compute_impact`.

    The two exposure numbers are intentionally named with their definitions
    embedded in the field names so the LLM shell (or any caller) cannot
    confuse them with revenue forecasts.

    Compliance fields are populated by joining canonical_affected against
    the corridor's RegulationMap (REGULATION.md §3 — the 0% freeze)."""
    change: ChangeRequest
    canonical_affected: tuple[AffectedFare, ...]
    skipped: tuple[AffectedFare, ...]
    blast_radius_pairs: tuple[BlastRadiusPair, ...]
    inversions: tuple[FareInversion, ...]
    per_flow_exposure_pence: int       # static-demand sum across distinct repriced fares
    per_pair_exposure_pence: int       # cluster-weighted; GB-map view only, NOT revenue
    notes: tuple[str, ...]             # assumptions + honest gaps
    # --- Compliance (REGULATION.md §3) -----------------------------------
    regulated_count: int               # canonical rows with compliance.status != 'not_regulated'
    breach_count: int                  # canonical rows with compliance.status == 'breach'
    breaches: tuple[AffectedFare, ...] # subset of canonical_affected flagged as breach
    regulation_map_notes: tuple[str, ...]  # the regmap's own disclosures (§4 baseline fallback etc.)


def compute_impact(
    change: ChangeRequest,
    feed_paths: FeedPaths,
    *,
    regulation_map: RegulationMap | None = None,
) -> ImpactReport:
    """Orchestrate: validate → affected set → compliance join → inversions → exposure → assemble.

    `regulation_map` is built per-corridor from the change if not supplied
    (the common path). Callers that want to inject a pre-built map (the
    LLM shell sharing one map across sibling changes) can pass it in."""
    validation = validate_against_feed(change, feed_paths)
    if not validation.ok:
        # Boundary failure — raise rather than return a bogus report.
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

    affected_set = compute_affected_set(change, feed_paths)
    notes.extend(affected_set.notes)

    # Compliance join (REGULATION.md §3). Build the regmap once per call.
    # The regmap is keyed by the CORRIDOR NLCs (not the per-row
    # representative pair), so we pass them through explicitly.
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

    inversions = detect_inversions(affected_set.canonical, feed_paths)

    per_flow = per_flow_exposure(affected_set.canonical)
    per_pair = per_pair_exposure(affected_set.canonical)

    breaches = tuple(
        f for f in affected_set.canonical
        if f.compliance is not None and f.compliance.status == "breach"
    )
    regulated = sum(
        1 for f in affected_set.canonical
        if f.compliance is not None and f.compliance.status != "not_regulated"
    )

    return ImpactReport(
        change=change,
        canonical_affected=affected_set.canonical,
        skipped=affected_set.skipped,
        blast_radius_pairs=affected_set.blast_radius,
        inversions=inversions,
        per_flow_exposure_pence=per_flow,
        per_pair_exposure_pence=per_pair,
        notes=tuple(notes),
        regulated_count=regulated,
        breach_count=len(breaches),
        breaches=breaches,
        regulation_map_notes=regmap.notes,
    )


__all__ = ["ImpactReport", "compute_impact"]
