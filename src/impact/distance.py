"""Per-flow rail route distance, with the derivation method recorded.

Two methods, tried in order of fidelity:

  1. "rgd_shortest_path" — Dijkstra over the RSPS5047 .RGD station-link
     file (§ 6.9: physical distances between adjacent stations, decimal
     miles). The shortest rail path through the physical network is real
     routeing-guide mileage. The plan's original idea — chaining .RGD
     links along one serving train's calling points — fails for any
     train that skips stations (consecutive CALLS are rarely ADJACENT
     stations), so the graph shortest-path is the faithful use of the
     same source data.
  2. "great_circle_x1.2" — straight-line distance from the RSPS5046
     .MSN master-station OSGB grid references, times a 1.2 circuity
     uplift (the public convention for approximating rail route length
     from crow-flies distance). On GB scales, planar distance on the
     National Grid is within ~0.1% of the true great circle, so we
     compute it directly in BNG metres.

Every result says WHICH method produced it — the carbon validation
gates diagnose distance errors by exactly this field. No method
available → None, never a guess.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from src.ingest.inspect import _cached

if TYPE_CHECKING:
    from src.api.geo import StationCoord
from src.ingest.routeing import load_station_link_distances

MILES_TO_KM = 1.609344

# Circuity uplift applied to crow-flies distance. Public convention for
# GB rail (route length ≈ 1.2 x straight line); a named, visible
# assumption per CLAUDE.md — flagged in every result that uses it.
GREAT_CIRCLE_UPLIFT = 1.2

DistanceMethod = Literal["rgd_shortest_path", "great_circle_x1.2"]


@dataclass(frozen=True)
class DistanceResult:
    km: float
    method: DistanceMethod
    notes: tuple[str, ...]


# --- .RGD graph ------------------------------------------------------------


def _build_rgd_graph(rgd_path: Path) -> dict[str, tuple[tuple[str, float], ...]]:
    """Undirected adjacency map CRS -> ((neighbour_crs, km), ...).

    Rows with a non-numeric distance are dropped (quarantine-by-omission;
    the .RGD is machine-generated so this should be rare) — consistent
    with never guessing a distance."""
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for link in load_station_link_distances(rgd_path):
        try:
            km = float(link.distance) * MILES_TO_KM
        except ValueError:
            continue
        if km < 0:
            continue
        adjacency.setdefault(link.from_crs, []).append((link.to_crs, km))
        adjacency.setdefault(link.to_crs, []).append((link.from_crs, km))
    return {crs: tuple(edges) for crs, edges in adjacency.items()}


def _rgd_shortest_path_km(
    graph: dict[str, tuple[tuple[str, float], ...]],
    origin_crs: str,
    dest_crs: str,
) -> float | None:
    """Dijkstra over the station-link graph. None when either endpoint is
    absent from the .RGD or the two sit in disconnected components."""
    if origin_crs not in graph or dest_crs not in graph:
        return None
    best: dict[str, float] = {origin_crs: 0.0}
    heap: list[tuple[float, str]] = [(0.0, origin_crs)]
    while heap:
        dist, node = heapq.heappop(heap)
        if node == dest_crs:
            return dist
        if dist > best.get(node, float("inf")):
            continue
        for neighbour, edge_km in graph.get(node, ()):
            candidate = dist + edge_km
            if candidate < best.get(neighbour, float("inf")):
                best[neighbour] = candidate
                heapq.heappush(heap, (candidate, neighbour))
    return None


# --- Great-circle fallback ---------------------------------------------------


def _crow_flies_km(a: StationCoord, b: StationCoord) -> float:
    """Planar distance in BNG metres (MSN grid refs are 100 m units with
    fixed offsets — see src/api/geo.py, verified against Euston)."""
    de = (a.easting - b.easting) * 100.0
    dn = (a.northing - b.northing) * 100.0
    return (de * de + dn * dn) ** 0.5 / 1000.0


# --- Public entry ------------------------------------------------------------


def flow_distance_km(
    origin_crs: str,
    dest_crs: str,
    *,
    rgd_path: Path | None,
    msn_path: Path | None,
) -> DistanceResult | None:
    """Route distance between two stations by CRS. Tries .RGD shortest
    path, falls back to great-circle x uplift, returns None when neither
    source can answer (caller must degrade with a note, not invent)."""
    if origin_crs == dest_crs:
        return None

    if rgd_path is not None and rgd_path.exists():
        graph = _cached(Path(rgd_path), _build_rgd_graph)
        km = _rgd_shortest_path_km(graph, origin_crs, dest_crs)
        if km is not None:
            return DistanceResult(
                km=round(km, 1),
                method="rgd_shortest_path",
                notes=(
                    f"shortest path over RSPS5047 .RGD station links "
                    f"({rgd_path.name}); real routeing-guide mileage, "
                    "assumes the shortest physical rail path.",
                ),
            )

    if msn_path is not None and msn_path.exists():
        # Deferred: src.api.geo is pure/file-only, but importing it triggers
        # src.api.__init__ -> main -> schemas -> src.impact.report (cycle).
        from src.api.geo import load_station_coords

        coords = load_station_coords(msn_path)
        a = coords.get(origin_crs)
        b = coords.get(dest_crs)
        if a is not None and b is not None:
            km = _crow_flies_km(a, b) * GREAT_CIRCLE_UPLIFT
            return DistanceResult(
                km=round(km, 1),
                method="great_circle_x1.2",
                notes=(
                    f"crow-flies from .MSN OSGB grid refs x {GREAT_CIRCLE_UPLIFT} "
                    "circuity uplift (public convention) — an APPROXIMATION, "
                    "used because no .RGD path links these stations.",
                ),
            )

    return None


__all__ = [
    "DistanceMethod",
    "DistanceResult",
    "GREAT_CIRCLE_UPLIFT",
    "MILES_TO_KM",
    "flow_distance_km",
]
