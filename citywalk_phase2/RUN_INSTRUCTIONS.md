# City Walk Phase 2 — Run Locally

This runs on your own machine, not in the sandbox — the server needs to bind a port your
browser can actually reach, which a sandboxed environment can't expose.

## 1. Install dependencies (no admin rights needed)

```
pip install eclipse-sumo fastapi "uvicorn[standard]" --break-system-packages
```

(Drop `--break-system-packages` if you're in a virtualenv.)

## 2. Set SUMO_HOME and PYTHONPATH

```
export SUMO_HOME=$(python3 -c "import sumo, os; print(os.path.dirname(sumo.__file__))")
export PYTHONPATH="$SUMO_HOME/tools:$PYTHONPATH"
```

(On Windows, use `set` / PowerShell equivalents, or just add these to your shell profile.)

## 3. Run the server

From this folder:

```
python3 server.py
```

It starts SUMO headless via TraCI, steps the City Walk simulation, and serves both the
Cesium client and the WebSocket feed on port 8000.

## 4. Open it

Go to `http://localhost:8000/` in a browser. You should see the City Walk building mesh,
then vehicles (orange boxes) appear and move along real Dubai roads as SUMO simulates
them. The HUD in the top-left shows sim time and live vehicle count.

The simulation loops automatically when all vehicles finish their routes, so it runs
continuously for demo purposes.

## Files

- `server.py` — FastAPI + TraCI, runs the sim and broadcasts vehicle state over `/ws`
- `cesium_traffic.html` — Cesium client, renders the building mesh + live vehicles
- `citywalk.net.xml` / `citywalk.rou.xml` — the SUMO network and routes (from Phase 1)
- `citywalk_buildings.glb` — the extruded building mesh (from Phase 2 Step 1)
