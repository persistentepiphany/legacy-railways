"""Corridor calling-points endpoint for the map's spine + corridor strip.

  GET /api/corridor/callings?origin=MAN&dest=EUS → CorridorCallingsModel

Returns the REAL stations that corridor trains call at, in calling order,
derived from the RSPS5046 timetable snapshot (reusing the same
`trains_serving_corridor` scan the splits module uses — no new parsing).

Ordering: `intermediate_calls` alone returns an alphabetical union, which is
useless for drawing a spine. Here each intermediate is placed at its mean
normalized position (0=origin, 1=dest) across all serving trains, which is a
deterministic, timetable-faithful calling order even when different services
skip different stops.

If no through service links the pair in either direction, that's a typed
`found=False` miss — we never stitch or fabricate a calling pattern.

Schemas live here (not schemas.py) to keep this module self-contained.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from src.impact.feed_paths import FeedPaths

router = APIRouter()


class CallingPointModel(BaseModel):
    crs: str
    nlc: str | None = None
    name: str | None = None
    x: float | None = None
    y: float | None = None
    trains_calling: int = 0


class CorridorCallingsModel(BaseModel):
    found: bool
    reason: str | None = None
    origin_crs: str
    dest_crs: str
    callings: list[CallingPointModel] = []
    direct_trains: int = 0
    reversed_path: bool = False
    source: str | None = None


# Keyed by (mca path, origin, dest). load_timetable_index already caches the
# parsed index on (path, mtime, size); this just skips the linear re-scan.
_cache: dict[tuple[str, str, str], CorridorCallingsModel] = {}


def _ordered_callings(trains, a: str, b: str) -> list[tuple[str, float, int]]:
    """(crs, mean normalized position, trains calling) for stations strictly
    between `a` and `b`, ordered by position along the corridor."""
    pos_sum: dict[str, float] = {}
    count: dict[str, int] = {}
    for s in trains:
        seq = [cp.crs for cp in s.calling_points if cp.crs]
        try:
            i = seq.index(a)
            j = seq.index(b, i + 1)
        except ValueError:
            continue
        span = j - i
        for k in range(i + 1, j):
            crs = seq[k]
            pos_sum[crs] = pos_sum.get(crs, 0.0) + (k - i) / span
            count[crs] = count.get(crs, 0) + 1
    out = [(crs, pos_sum[crs] / count[crs], count[crs]) for crs in pos_sum]
    out.sort(key=lambda t: (t[1], t[0]))
    return out


@router.get("/api/corridor/callings", response_model=CorridorCallingsModel)
def api_corridor_callings(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3),
    dest: str = Query(..., min_length=3, max_length=3),
) -> CorridorCallingsModel:
    o, d = origin.upper(), dest.upper()

    def miss(reason: str) -> CorridorCallingsModel:
        return CorridorCallingsModel(found=False, reason=reason,
                                     origin_crs=o, dest_crs=d)

    if o == d:
        return miss("origin and destination are the same station")

    fp: FeedPaths = request.app.state.feed_paths
    mca = fp.timetable_mca
    if mca is None or not mca.exists():
        return miss("no RSPS5046 timetable (.MCA) in data/ — cannot derive calling points")

    key = (str(mca), o, d)
    if key in _cache:
        return _cache[key]

    from src.ingest.timetable import load_timetable_index, trains_serving_corridor

    idx = load_timetable_index(mca)
    reversed_path = False
    a, b = o, d
    trains = trains_serving_corridor(idx, a, b)
    if not trains:
        a, b = d, o
        trains = trains_serving_corridor(idx, a, b)
        reversed_path = True
    if not trains:
        return miss(f"no through service links {o} and {d} in timetable "
                    f"snapshot {idx.source_file}")

    n_trains = len(trains)
    ordered = [(a, 0.0, n_trains)] + _ordered_callings(trains, a, b) \
        + [(b, 1.0, n_trains)]
    if reversed_path:
        ordered = [(crs, 1.0 - pos, n) for crs, pos, n in reversed(ordered)]

    # CRS→NLC join, same as /api/route: a calling point without a fares NLC
    # still renders on the spine but can't anchor a split or highlight.
    from src.ingest.inspect import load_loc_meta
    crs_to_nlc: dict[str, str] = {}
    if fp.loc.exists():
        for nlc, meta in load_loc_meta(fp.loc).items():
            crs = getattr(meta, "crs", None)
            if crs and crs not in crs_to_nlc:
                crs_to_nlc[crs] = nlc

    stations = request.app.state.stations
    callings: list[CallingPointModel] = []
    for crs, _pos, n in ordered:
        st = stations.get(crs)
        callings.append(CallingPointModel(
            crs=crs,
            nlc=crs_to_nlc.get(crs),
            name=(st.name.title() if st and st.name else None),
            x=(st.x if st else None),
            y=(st.y if st else None),
            trains_calling=n,
        ))

    result = CorridorCallingsModel(
        found=True,
        origin_crs=o, dest_crs=d,
        callings=callings,
        direct_trains=n_trains,
        reversed_path=reversed_path,
        source=idx.source_file,
    )
    _cache[key] = result
    return result
