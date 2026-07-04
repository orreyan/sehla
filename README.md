# Dubai Traffic Twin — City Walk (Phase 2)

A small-scale, real-data digital twin of City Walk, Dubai: real OpenStreetMap roads and
buildings, a live SUMO microscopic traffic simulation, and a 3D CesiumJS viewer that
renders vehicles and traffic signals in real time over a WebSocket feed.

This is Phase 2 of a 5-phase roadmap (2D SUMO sim → 3D CesiumJS twin → real-world data
ingestion → reinforcement learning → LLM orchestration/dashboard). This repo covers the
end of Phase 1 and all of Phase 2, scoped to a small test area (City Walk) before scaling
up to full Downtown Dubai.

## What's in here

```
map.osm                    Raw OSM export for the City Walk bounding box
citywalk_phase2/
  citywalk.net.xml         SUMO road network (built from map.osm via netconvert)
  citywalk.rou.xml         SUMO vehicle routes/trips
  citywalk.trips.xml       Trip definitions used to generate the routes
  build_mesh.py            Extrudes OSM building footprints into a 3D mesh (glTF/GLB)
  citywalk_buildings.glb   Pre-built output of build_mesh.py
  server.py                FastAPI + TraCI server: runs the sim, streams state over WebSocket
  cesium_traffic.html      CesiumJS client: renders buildings, live vehicles, traffic lights
  RUN_INSTRUCTIONS.md      Quick local run guide (subset of this README)
```

## How it fits together

1. `map.osm` (real OSM data for City Walk) is compiled into a routable road network
   (`citywalk.net.xml`) using SUMO's `netconvert`.
2. `build_mesh.py` reads the same `map.osm` and extrudes every tagged building footprint
   into a 3D mesh, exported as `citywalk_buildings.glb`.
3. `server.py` runs the SUMO simulation headless via TraCI, steps it continuously, and
   broadcasts vehicle positions/headings and per-lane traffic signal states over a
   WebSocket at ~5Hz.
4. `cesium_traffic.html` loads the building mesh, connects to that WebSocket, and renders
   vehicles as oriented 3D boxes and traffic signals as one colored dot per controlled
   lane (not one dot per intersection — real intersections have simultaneous red and
   green approaches, so it's modeled per-lane).

## Prerequisites

- Python 3.9+
- No admin rights needed; everything installs via pip

## Setup — run the existing simulation

This runs on your own machine, not in a sandbox — the server binds a local port your
browser needs to reach directly.

```bash
cd citywalk_phase2

pip install eclipse-sumo fastapi "uvicorn[standard]" --break-system-packages
# (drop --break-system-packages if you're using a virtualenv)

export SUMO_HOME=$(python3 -c "import sumo, os; print(os.path.dirname(sumo.__file__))")
export PYTHONPATH="$SUMO_HOME/tools:$PYTHONPATH"

python3 server.py
```

Then open `http://localhost:8000/` in a browser. You should see the City Walk building
mesh load, then orange vehicle boxes appear and move along real roads, with colored dots
at intersections showing live signal state. The HUD (top-left) shows sim time, vehicle
count, and signal count. The simulation loops automatically when all vehicles finish
their routes, so it runs continuously.

Drag to pan, scroll to zoom, right-drag (or ctrl+drag) to tilt/look around.

## Rebuilding from scratch (regenerating network / routes / mesh)

You only need this if you change `map.osm` itself or want to regenerate demand/mesh with
different parameters. Requires the SUMO toolset (`netconvert`, `randomTrips.py`) from
`eclipse-sumo`, plus `trimesh`, `shapely`, and `numpy` for the mesh step:

```bash
pip install eclipse-sumo trimesh shapely numpy --break-system-packages
export SUMO_HOME=$(python3 -c "import sumo, os; print(os.path.dirname(sumo.__file__))")
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$SUMO_HOME/tools:$PYTHONPATH"
```

**1. Road network** (`map.osm` → `citywalk.net.xml`):

```bash
netconvert --osm-files map.osm -o citywalk.net.xml \
  --geometry.remove --roundabouts.guess --ramps.guess --junctions.join \
  --tls.guess-signals --tls.discard-simple --tls.join \
  --keep-edges.by-vclass passenger
```

**2. Traffic demand** (`citywalk.net.xml` → `citywalk.trips.xml` / `citywalk.rou.xml`):

```bash
python3 "$SUMO_HOME/tools/randomTrips.py" -n citywalk.net.xml \
  -o citywalk.trips.xml -r citywalk.rou.xml \
  -e 600 -p 6 --validate --fringe-factor 5
```

`-p` is the insertion period (seconds between vehicle insertions) — lower values pack in
more traffic. A higher-density run (`-p 1.5`) caused permanent gridlock on this small
network (SUMO teleport warnings for vehicles stuck in jams); `-p 6` with `--fringe-factor
5` gives a light, realistic flow with 0 teleports.

**3. Building mesh** (`map.osm` → `citywalk_buildings.glb`):

```bash
python3 build_mesh.py
```

Origin for the Cesium anchor is printed at the end (bbox center of the OSM extract) —
it must match the `originLon`/`originLat` constants near the top of
`cesium_traffic.html` if you re-export for a different bounding box.

## City Walk bounding box used for this extract

`55.2565,25.2035` (SW) to `55.2695,25.2115` (NE) — exported from
`openstreetmap.org/export`.

## Notes on `build_mesh.py`

- Building footprints whose centroid falls outside the fetched bbox are dropped. OSM's
  export API doesn't clip ways at the box edge — a building that straddles the boundary
  is included in full, but its real neighbors just outside the box are not, so it would
  otherwise render as an isolated structure standing in empty space. Cropping to the bbox
  avoids that.
- When a `building=yes` outer footprint has no height/level tags of its own and fully
  contains a `building:part` that does, only the part is extruded (skipping the
  default-height outer shell) so towers with detailed massing don't get double-extruded
  into two overlapping shapes.

## Known limitations

- Scoped to a small test area (City Walk), not the full Downtown Dubai region in the
  original roadmap — this was a deliberate first step to validate the pipeline before
  committing to the larger export.
- Traffic signal markers are placed ~8m back from each lane's stop line, one per
  controlled link, so closely-spaced lanes can visually cluster when zoomed far out
  (mitigated with distance-based scaling/fading, but inherent to real lane spacing).
- The building mesh is a lightweight custom extrusion pipeline (footprint → height, via
  `trimesh`), not a full architectural reconstruction like OSM2World — flat roofs and
  extruded footprints only, no roof shapes, facades, or windows.
