"""Performance overlay module — HSP three-mode fetcher (live -> cached -> fixture).

Server-side only. HSP has no CORS; the browser must never call it directly."""

from src.perf.hsp import (
    PerformanceResult,
    ServicePerformance,
    ServiceTolerance,
    fetch_performance,
)

__all__ = [
    "PerformanceResult",
    "ServicePerformance",
    "ServiceTolerance",
    "fetch_performance",
]
