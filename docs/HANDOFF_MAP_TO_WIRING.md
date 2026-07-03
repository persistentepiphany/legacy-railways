# Handoff — real GB map ↔ backend station projection

**Audience**: whoever's wiring `frontend/live/` to the FastAPI backend
(the parallel session working on `rfe.api.js`, `rfe.store.js`, `rfe.adapt.js`,
`rfe.actions.js`, `/api/snapshot`, `/api/corridors`, `/api/stations`,
`/api/railcards`, `/api/events`, and `src/api/geo.py`).

**Author's scope**: only the map panel's geometry — the coastline outline and
its projection contract. Everything else (endpoints, store, adapters, wiring)
is yours. This note explains what I've landed and the one thing I need
from you.

---

## What's landed

1. **Old mocks archived**. `frontend/mockup/` (v1/v2/v3 Bloomberg-era) moved
   to `frontend/_archive/mockup_bloomberg/`. The two source zips
   (`Bloomberg terminal for fares.zip`, `UK Train Fare Platform.zip`) moved
   to `frontend/_archive/originals/`. Nothing deleted, nothing referenced by
   the live tree. Comparison grid at
   `frontend/_archive/mockup_bloomberg/index.html` still works if you serve
   the archive dir.

2. **Delivered UI unpacked** at `frontend/live/` — `index.html` is a
   verbatim copy of the delivered `.dc.html`, plus `support.js` and
   `fare-engine.js` untouched otherwise. You've already added `rfe.api.js`
   here — I didn't touch it. If you re-unpack the delivered zip and blow
   my one surgical edit away, see §"Reproducing my surgery" below.

3. **Real GB coastline asset** at `frontend/live/assets/gb-outline.{json,js}`.
   - `.json` — the canonical form. Fields: `viewbox`, `bbox_bng`,
     `projection {scale, offset_x, offset_y, y_flipped}`, `d` (SVG path
     string), `source`, `simplify_tolerance_m`.
   - `.js` — a synchronous wrapper that sets `window.__RFE_GB` from the
     same payload. Loaded before `fare-engine.js` so the current mock's
     `buildMap()` picks it up on first render with zero async coordination.
   - Regen: `python tools/build_gb_outline.py` — sources
     martinjc/UK-GeoJSON EER (WGS84) → dissolves → reprojects to
     British National Grid (EPSG:27700) → simplifies → renders to viewBox
     420×640. Attribution + licence at `frontend/live/assets/GEO_LICENCE.md`.

4. **One 2-line edit to `frontend/live/index.html`**:
   - Added `<script src="assets/gb-outline.js"></script>` in `<helmet>`.
   - Changed `buildMap()`'s `outline` constant to
     `(window.__RFE_GB && window.__RFE_GB.d) || <fallback blob>` so the
     original stylised blob still shows if the asset fails to load.

That's it. No changes to `fare-engine.js`, `support.js`, or `rfe.api.js`.
No new panels. No new colour tokens. Colour palette / typography / hairline
strokes / zoom-pan controls / tooltip / legend all untouched.

---

## The one thing I need from you

The coastline is drawn in a specific OSGB-Easting/Northing → SVG viewBox
transform. **Your `/api/stations` must place station dots through the
identical transform**, or the dots float in the sea.

**Contract** — every station's projected SVG (x, y) must satisfy:

```
x = 54.010 + (easting_bng - 71421.11) * 0.0005006
y =  4.000 + (1195232.03 - northing_bng) * 0.0005006
                   ↑ y is flipped (SVG y grows downward)
```

Numbers come from `frontend/live/assets/gb-outline.json`'s `projection`
block and `bbox_bng`. **Do not re-derive them independently** — read them
from the JSON at startup and burn them into `src/api/geo.py` as the single
source of truth. If we ever regen the coastline the projection shifts and
both sides need to pick up the new constants together; wiring them from
the same file makes that free.

Suggested `src/api/geo.py` sketch:

```python
import json
from pathlib import Path

_ASSET = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend" / "live" / "assets" / "gb-outline.json"
)

def _projection():
    p = json.loads(_ASSET.read_text())
    minE, minN, maxE, maxN = p["bbox_bng"]
    return {
        "scale": p["projection"]["scale"],
        "off_x": p["projection"]["offset_x"],
        "off_y": p["projection"]["offset_y"],
        "minE": minE, "minN": minN, "maxE": maxE, "maxN": maxN,
    }

_PROJ = _projection()  # module-level cache

def project_bng_to_viewbox(easting: float, northing: float) -> tuple[float, float]:
    p = _PROJ
    return (
        p["off_x"] + (easting - p["minE"]) * p["scale"],
        p["off_y"] + (p["maxN"] - northing) * p["scale"],
    )
```

Round trip should land Kings Cross (approx BNG 530200, 182900) at roughly
(283, 511) in SVG viewBox space — inside the coastline near London.

---

## Where OSGB coords come from

The `.LOC` file (`data/RJFAF805.LOC`) is the authoritative source. RSPS5045
§4.10 lays out the record format. `src/ingest/inspect.py::parse_loc` already
extracts NLC/CRS/name/county — you'll need to extend it (or write a sibling
parser) to also pull Easting/Northing. Look at the fixed-width offsets in
the spec; they land somewhere in the ~78–100 range on 'RL0' records. I
didn't parse them because that's endpoint scope, not map-outline scope.

If the .LOC coords are missing/blank/zero for some stations (feed
pathology), fall back to `.MSN` (`data/RJTTF883.MSN`) which is timetable-side
and has grid refs on all active operational stations.

Quarantine any station where both are unusable — surface it via
`notes` on the impact response or a resolver-quarantine event, not a
silent absence. Standard fares-cockpit "never fabricate" discipline.

---

## Runtime behaviour I'm relying on

- `<script src="./support.js">` in `<head>` initialises the delivered
  x-dc / React-under-support.js runtime.
- `<script src="assets/gb-outline.js">` sets `window.__RFE_GB`
  synchronously before…
- `<script src="fare-engine.js">` runs, which registers `window.RFE`.
- Component instantiates, calls `buildMap()`, reads `window.__RFE_GB.d`
  → real coastline on first paint.

If you replace `fare-engine.js` with your store-backed rewrite, **preserve
this contract**: consume `window.__RFE_GB.d` for the map's outline. Or
switch to fetching `assets/gb-outline.json` yourself in your init pipeline
and dispatching an action that sets `state.map.outline = payload.d`. Either
works; the JSON is the canonical asset.

---

## Reproducing my surgery (if you clobber `index.html`)

Two edits, ~4 lines total:

```diff
 <script src="./support.js"></script>
 ...
+<script src="assets/gb-outline.js"></script>
 <script src="fare-engine.js"></script>
 </helmet>
```

and at ~line 1644 of `index.html`, inside `buildMap()`:

```diff
-  const outline = 'M182 78 C196 70 214 74 ... 172 96 C160 88 168 80 182 78 Z';
+  const outline = (window.__RFE_GB && window.__RFE_GB.d)
+    || 'M182 78 C196 70 214 74 ... 172 96 C160 88 168 80 182 78 Z';
```

Fallback preserved on purpose — if the asset ever 404s in a demo, the panel
degrades gracefully to the stylised blob rather than rendering empty.

---

## Known-not-fixed-yet

- **Fixture stations sit at hand-picked x/y** that were tuned to the old
  stylised blob, not real geography. On the real coastline they land in
  approximately-plausible-but-often-wrong positions (Glasgow ends up on
  Scotland's east side, Birmingham on the west coast). This resolves the
  moment your `/api/stations` starts feeding real projected coords through
  the transform above. Until then, the coastline is real but the dots are
  wrong — a temporary state that's obvious to the reader and clearly
  attributed to "waiting for /api/stations".
- **Default viewBox crops most of the coastline**. The delivered UI's
  `mapViewBox()` at zoom 1 shows a 150×414 window centred at (225, 279)
  — an England-focused framing that's great for Manchester–Euston but
  cuts off Scotland. Reset button already exists (top-right of the map).
  If you want the default to show all of GB, change `mapViewBox()` at
  ~line 1275 to compute `w = 420 / z, h = 640 / z, cx = 210 + p.x,
  cy = 320 + p.y` — one-line change, but I left the delivered default
  alone since it's a UX call, not a map-scope call.

---

## Contact surface

- Coastline regen: `python tools/build_gb_outline.py`
- Coastline sanity check: SVG preview at `/tmp/gb-preview.svg` (writeable
  by rerunning the build script + a small snippet — see conversation log).
- Projection constants: `frontend/live/assets/gb-outline.json.projection`.
- Attribution: `frontend/live/assets/GEO_LICENCE.md`.

Ping me if you want a different projection (e.g. mercator, or a different
viewBox aspect) — swapping the pipeline in `tools/build_gb_outline.py`
takes ~10 minutes and I'd rather do it once cleanly than have both sides
drift.
