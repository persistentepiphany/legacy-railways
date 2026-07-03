# GB coastline asset — provenance & licence

## File
- `gb-outline.json` — SVG path pre-projected to the cockpit map's viewBox
  (`0 0 420 640`) from British National Grid (EPSG:27700) coordinates. Built
  by `tools/build_gb_outline.py`. Not hand-edited — regenerate via that script.

## Source
- `martinjc/UK-GeoJSON`, file `json/electoral/gb/topo_eer.json`
  (European Electoral Regions, GB coverage, WGS84).
  https://github.com/martinjc/UK-GeoJSON — released under the MIT Licence.

## Underlying data
The martinjc file is derived from **Office for National Statistics**
Open Geography Portal data:
- Contains OS data © Crown copyright and database right 2013.
- Contains public sector information licensed under the
  **Open Government Licence v3.0**:
  https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/

## Attribution when reproducing the map
"Contains OS data © Crown copyright and database right [year]. Contains
public sector information licensed under the Open Government Licence v3.0.
Boundaries derived from ONS Open Geography Portal via
[martinjc/UK-GeoJSON](https://github.com/martinjc/UK-GeoJSON)."

Attribution belongs in the cockpit's about/help surface, not on the map
itself — the map is intentionally minimal.
