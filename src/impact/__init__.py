"""Impact engine — turn a ChangeRequest into a full ImpactReport.

Pure, deterministic, side-effect-free. Reads the feed via mtime-cached
loaders; produces a report aggregating affected set + blast radius + a
suite of optional analysis blocks (compliance, anomalies, revenue, splits).
The LLM never computes anything in this path; the ChangeRequest is
constructed directly in code.

Public API:
    compute_impact      — the orchestrator (single public entry point)
    ChangeRequest       — proposal dataclass
    ImpactReport        — output dataclass (substrate + Optional blocks)
    AnomaliesBlock      — Optional block: structural anomalies
    ComplianceBlock     — Optional block: regulation compliance
    RevenueBlock        — Optional block: structural exposure
    SplitOpportunityResult — Optional block: split-ticket arbitrage
    DEFAULT_INCLUDE     — frozenset of include keys computed by default
    KNOWN_INCLUDE_KEYS  — every legal include key
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
from src.impact.compliance import (
    ComplianceStatus,
    ComplianceVerdict,
    attach_compliance,
    build_corridor_regulation_map,
    check_compliance,
)
from src.impact.feed_paths import FeedPaths
from src.impact.inversions import FareInversion, detect_inversions
from src.impact.odm import (
    ODMIndex,
    ODMRevenueBlock,
    ODMRevenueEstimate,
    compute_odm_revenue,
    load_odm_index,
)
from src.impact.report import (
    DEFAULT_INCLUDE,
    KNOWN_INCLUDE_KEYS,
    AnomaliesBlock,
    ComplianceBlock,
    ImpactReport,
    PerformanceBlock,
    RevenueBlock,
    compute_impact,
)
from src.impact.revenue import per_flow_exposure, per_pair_exposure
from src.impact.splits import (
    DEMO_CORRIDOR_INTERMEDIATES,
    SplitCandidate,
    SplitOpportunityResult,
    SplitStatus,
    detect_splits,
    splits_for_change,
)
from src.impact.synthetic_railcard import (
    apply_synthetic_railcard,
    inject_synthetic_railcard,
)

__all__ = [
    "AffectedFare",
    "AffectedSet",
    "AnomaliesBlock",
    "BlastRadiusPair",
    "ChangeRequest",
    "ComplianceBlock",
    "ComplianceStatus",
    "ComplianceVerdict",
    "DEFAULT_INCLUDE",
    "DEMO_CORRIDOR_INTERMEDIATES",
    "FareInversion",
    "FeedPaths",
    "ImpactReport",
    "KNOWN_INCLUDE_KEYS",
    "ODMIndex",
    "ODMRevenueBlock",
    "ODMRevenueEstimate",
    "PerformanceBlock",
    "RevenueBlock",
    "SplitCandidate",
    "SplitOpportunityResult",
    "SplitStatus",
    "apply_synthetic_railcard",
    "attach_compliance",
    "build_corridor_regulation_map",
    "check_compliance",
    "compute_affected_set",
    "compute_impact",
    "compute_odm_revenue",
    "detect_inversions",
    "detect_splits",
    "inject_synthetic_railcard",
    "load_odm_index",
    "per_flow_exposure",
    "per_pair_exposure",
    "splits_for_change",
    "validate_against_feed",
]
