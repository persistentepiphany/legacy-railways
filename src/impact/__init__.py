"""Impact engine — turn a ChangeRequest into a full ImpactReport.

Pure, deterministic, side-effect-free. Reads the feed via mtime-cached
loaders; produces a report aggregating affected set + blast radius +
inversions + revenue exposure. The LLM never computes anything in this
path; the ChangeRequest is constructed directly in code.

Public API:
    compute_impact      — the orchestrator (single public entry point)
    ChangeRequest       — proposal dataclass
    ImpactReport        — output dataclass
    AffectedFare        — one canonical repriced fare
    BlastRadiusPair     — one (o,d) reachable through cluster fan-out
    FareInversion       — one structural inversion detected post-change
    FeedPaths           — bundle of 9 RDG feed paths
"""

from src.impact.affected import (
    AffectedFare,
    AffectedSet,
    BlastRadiusPair,
    compute_affected_set,
)
from src.impact.change_request import ChangeRequest, validate_against_feed
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.impact.report import ImpactReport, compute_impact
from src.impact.revenue import per_flow_exposure, per_pair_exposure
from src.impact.synthetic_railcard import (
    apply_synthetic_railcard,
    inject_synthetic_railcard,
)

__all__ = [
    "AffectedFare",
    "AffectedSet",
    "BlastRadiusPair",
    "ChangeRequest",
    "FareInversion",
    "FeedPaths",
    "ImpactReport",
    "apply_synthetic_railcard",
    "compute_affected_set",
    "compute_impact",
    "detect_inversions",
    "inject_synthetic_railcard",
    "per_flow_exposure",
    "per_pair_exposure",
    "validate_against_feed",
]
