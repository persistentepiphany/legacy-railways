"""Build the GB coastline asset for the MERIDIAN cockpit map.

Pipeline: fetch martinjc/UK-GeoJSON's European Electoral Regions layer (WGS84)
→ dissolve to a single GB polygon → reproject to British National Grid
(EPSG:27700, the coordinate space `src/api/geo.py` also uses for stations)
→ simplify → pre-render to viewBox 420×640 SVG path so the frontend can bind
it directly (no d3-geo, no topojson-client needed at runtime).

One-shot; idempotent; commit the output. Sources are MIT-licensed
(martinjc/UK-GeoJSON), derived from ONS Open Geography Portal (Contains OS
data © Crown copyright; public sector information licensed under the Open
Government Licence v3.0). Attribution recorded at
frontend/live/assets/GEO_LICENCE.md.

Run:  python tools/build_gb_outline.py

Emits:
  frontend/live/assets/gb-outline.json — { viewbox: [0,0,420,640],
    bbox_bng: [minE,minN,maxE,maxN], d: "M…", source: "..." }.
    Ready to bind straight to <path d="…">. Single source of truth for the
    OSGB→viewBox mapping (backend `src/api/geo.py` must consume `bbox_bng`
    and match `viewbox` to place its stations in the same space).
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import transform, unary_union


SOURCE_URL = (
    "https://raw.githubusercontent.com/martinjc/UK-GeoJSON/master/"
    "json/electoral/gb/topo_eer.json"
)
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "frontend" / "live" / "assets"
OUT_JSON = OUT_DIR / "gb-outline.json"
OUT_JS = OUT_DIR / "gb-outline.js"
CACHE = Path("/tmp/gb-outline/topo_eer.json")

# The cockpit map SVG the delivered UI uses. Keep in sync with the .dc.html.
VIEWBOX = (0.0, 0.0, 420.0, 640.0)
# A 4 px inset so the coastline never touches the SVG edge (visual breathing
# room, matches the recessive dark-outline idiom).
INSET = 4.0

# Simplify tolerance in BNG metres, applied twice — once pre-projection to
# fix source pathology, once post-projection to shrink the emitted `d` string.
SIMPLIFY_TOLERANCE_M = 4000.0
# Keep only the pieces the cockpit user will actually recognise: mainland GB
# plus the notable islands (Wight, Anglesey, Man, Skye, Lewis, Orkney,
# Shetland). 300 km² threshold cleanly filters everything else.
MIN_POLY_AREA_M2 = 300_000_000.0  # 300 km²
# Round emitted SVG coords to this many decimals; sub-pixel by 3+ orders.
COORD_DECIMALS = 2


def _fetch_source() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetch {SOURCE_URL}", file=sys.stderr)
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as r:
        data = r.read()
    CACHE.write_bytes(data)
    return json.loads(data)


def _topo_to_features(topo: dict) -> list[dict]:
    """Inline TopoJSON→GeoJSON — we only need the eer layer's polygons."""
    # TopoJSON arc-reassembly by hand (the `topojson` Python lib only writes).
    transform_ = topo.get("transform")
    if transform_:
        sx, sy = transform_["scale"]
        tx, ty = transform_["translate"]

        def decode_arc(arc):
            out = []
            x = y = 0
            for dx, dy in arc:
                x += dx
                y += dy
                out.append((x * sx + tx, y * sy + ty))
            return out
    else:
        def _identity(arc):
            return [tuple(pt) for pt in arc]
        decode_arc = _identity

    arcs = [decode_arc(a) for a in topo["arcs"]]

    def stitch(ring_arcs):
        pts: list[tuple[float, float]] = []
        for idx in ring_arcs:
            if idx < 0:
                seg = list(reversed(arcs[~idx]))
            else:
                seg = list(arcs[idx])
            if pts and pts[-1] == seg[0]:
                seg = seg[1:]
            pts.extend(seg)
        return pts

    features = []
    for g in topo["objects"]["eer"]["geometries"]:
        gt = g["type"]
        if gt == "Polygon":
            coords = [stitch(ring) for ring in g["arcs"]]
        elif gt == "MultiPolygon":
            coords = [[stitch(ring) for ring in poly] for poly in g["arcs"]]
        else:
            continue
        features.append({
            "type": "Feature",
            "properties": g.get("properties", {}),
            "geometry": {"type": gt, "coordinates": coords},
        })
    return features


def build() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("loading source TopoJSON…", file=sys.stderr)
    topo = _fetch_source()
    features = _topo_to_features(topo)
    print(f"  {len(features)} EER features", file=sys.stderr)

    print("dissolving to single GB outline…", file=sys.stderr)
    # buffer(0) repairs the invalid rings in the source (side-location conflict
    # on some Scottish island geometries — a well-known ONS/topo_eer quirk).
    geoms = [shape(f["geometry"]).buffer(0) for f in features]
    gb_wgs84 = unary_union(geoms)

    print(
        f"reprojecting WGS84 → EPSG:27700 (BNG), simplify {SIMPLIFY_TOLERANCE_M} m…",
        file=sys.stderr,
    )
    to_bng = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    gb_bng = transform(lambda x, y, _=None: to_bng.transform(x, y), gb_wgs84)
    gb_bng = gb_bng.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)

    # Drop the small isles that don't earn their bytes.
    def _drop_small_holes(poly: Polygon) -> Polygon:
        # buffer(0) repair generates thousands of sub-km² "holes" that are
        # topology noise, not real inland lakes. At cockpit zoom none is
        # visible. Keep the ext ring; drop interiors below the same
        # threshold used for polygons.
        interiors = [r for r in poly.interiors if Polygon(r).area >= MIN_POLY_AREA_M2]
        return Polygon(poly.exterior, interiors)

    if isinstance(gb_bng, MultiPolygon):
        kept = [_drop_small_holes(p) for p in gb_bng.geoms if p.area >= MIN_POLY_AREA_M2]
        hole_ct = sum(len(p.interiors) for p in kept)
        print(
            f"  kept {len(kept)}/{len(gb_bng.geoms)} polygons "
            f"(≥ {MIN_POLY_AREA_M2/1e6:.0f} km²), {hole_ct} kept holes",
            file=sys.stderr,
        )
        gb_bng = MultiPolygon(kept) if len(kept) > 1 else kept[0]
    elif isinstance(gb_bng, Polygon):
        gb_bng = _drop_small_holes(gb_bng)

    minx, miny, maxx, maxy = gb_bng.bounds
    print(
        f"  BNG bbox: E {minx:.0f}..{maxx:.0f}  N {miny:.0f}..{maxy:.0f}",
        file=sys.stderr,
    )

    # ---- project BNG → viewBox 420×640 (linear, preserve aspect, inset) ----
    vb_x, vb_y, vb_w, vb_h = VIEWBOX
    inner_w = vb_w - 2 * INSET
    inner_h = vb_h - 2 * INSET
    bng_w = maxx - minx
    bng_h = maxy - miny
    # Uniform scale (fits height for GB — height dominates aspect).
    scale = min(inner_w / bng_w, inner_h / bng_h)
    proj_w = bng_w * scale
    proj_h = bng_h * scale
    off_x = vb_x + INSET + (inner_w - proj_w) / 2
    off_y = vb_y + INSET + (inner_h - proj_h) / 2

    def project(e: float, n: float) -> tuple[float, float]:
        # Y-axis flipped (SVG y grows downward).
        return (off_x + (e - minx) * scale, off_y + (maxy - n) * scale)

    def coords_to_d(rings) -> str:
        parts: list[str] = []
        for ring in rings:
            first = True
            for e, n in ring:
                x, y = project(e, n)
                cmd = "M" if first else "L"
                parts.append(f"{cmd}{round(x, COORD_DECIMALS)},{round(y, COORD_DECIMALS)}")
                first = False
            parts.append("Z")
        return "".join(parts)

    def geom_to_rings(g):
        if isinstance(g, Polygon):
            return [list(g.exterior.coords), *(list(r.coords) for r in g.interiors)]
        if isinstance(g, MultiPolygon):
            rings = []
            for poly in g.geoms:
                rings.extend(geom_to_rings(poly))
            return rings
        raise TypeError(f"unexpected geometry: {g.geom_type}")

    d_string = coords_to_d(geom_to_rings(gb_bng))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "viewbox": list(VIEWBOX),
        "inset": INSET,
        "crs": "EPSG:27700",
        "bbox_bng": [minx, miny, maxx, maxy],
        "projection": {
            "scale": scale,
            "offset_x": off_x,
            "offset_y": off_y,
            "y_flipped": True,
        },
        "d": d_string,
        "source": "martinjc/UK-GeoJSON topo_eer.json → dissolved → EPSG:27700 → simplify → viewBox",
        "simplify_tolerance_m": SIMPLIFY_TOLERANCE_M,
    }
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":")))
    kb = OUT_JSON.stat().st_size / 1024
    print(f"wrote {OUT_JSON}  ({kb:.1f} KB, path {len(d_string)} chars)", file=sys.stderr)

    # Synchronous-load JS wrapper: makes window.__RFE_GB available before
    # fare-engine.js runs, so buildMap() can pick it up on first render
    # without any async re-render dance. Small, generated, do not hand-edit.
    OUT_JS.write_text(
        "/* generated by tools/build_gb_outline.py — do not edit by hand */\n"
        "window.__RFE_GB = " + json.dumps(payload, separators=(",", ":")) + ";\n"
    )
    kb = OUT_JS.stat().st_size / 1024
    print(f"wrote {OUT_JS}  ({kb:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    build()
