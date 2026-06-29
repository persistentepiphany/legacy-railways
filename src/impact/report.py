"""The ImpactReport dataclass + compute_impact public entry point.

`compute_impact(change, feed_paths) -> ImpactReport` is the single public
function the API surface / future LLM shell calls. Deterministic,
side-effect-free, surfaces honest gaps via `notes[]`."""

from __future__ import annotations

from dataclasses import dataclass

from src.impact.affected import AffectedFare, BlastRadiusPair, compute_affected_set
from src.impact.change_request import ChangeRequest, validate_against_feed
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.impact.revenue import per_flow_exposure, per_pair_exposure


@dataclass(frozen=True)
class ImpactReport:
    """Aggregate output of `compute_impact`.

    The two exposure numbers are intentionally named with their definitions
    embedded in the field names so the LLM shell (or any caller) cannot
    confuse them with revenue forecasts."""
    change: ChangeRequest
    canonical_affected: tuple[AffectedFare, ...]
    skipped: tuple[AffectedFare, ...]
    blast_radius_pairs: tuple[BlastRadiusPair, ...]
    inversions: tuple[FareInversion, ...]
    per_flow_exposure_pence: int       # static-demand sum across distinct repriced fares
    per_pair_exposure_pence: int       # cluster-weighted; GB-map view only, NOT revenue
    notes: tuple[str, ...]             # assumptions + honest gaps


def compute_impact(change: ChangeRequest, feed_paths: FeedPaths) -> ImpactReport:
    """Orchestrate: validate → affected set → inversions → exposure → assemble."""
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

    inversions = detect_inversions(affected_set.canonical, feed_paths)

    per_flow = per_flow_exposure(affected_set.canonical)
    per_pair = per_pair_exposure(affected_set.canonical)

    return ImpactReport(
        change=change,
        canonical_affected=affected_set.canonical,
        skipped=affected_set.skipped,
        blast_radius_pairs=affected_set.blast_radius,
        inversions=inversions,
        per_flow_exposure_pence=per_flow,
        per_pair_exposure_pence=per_pair,
        notes=tuple(notes),
    )


__all__ = ["ImpactReport", "compute_impact"]
