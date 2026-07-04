"""
Phase 2 Steps 2-3: FastAPI server that runs the City Walk SUMO simulation headless
via TraCI and broadcasts live vehicle positions/headings over WebSocket.

Run with:  python3 server.py
Requires SUMO_HOME set and <SUMO_HOME>/tools on PYTHONPATH (see run instructions).
"""
import asyncio
import json
import math
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

# --- make traci importable -------------------------------------------------
SUMO_HOME = os.environ.get("SUMO_HOME")
if not SUMO_HOME:
    try:
        import sumo
        SUMO_HOME = os.path.dirname(sumo.__file__)
    except ImportError:
        sys.exit("SUMO_HOME not set and 'sumo' package not importable. "
                 "Run: pip install eclipse-sumo")
sys.path.append(os.path.join(SUMO_HOME, "tools"))
import traci  # noqa: E402

NET_FILE = "citywalk.net.xml"
ROUTE_FILE = "citywalk.rou.xml"
STEP_LENGTH = 0.2       # seconds of sim time per TraCI step
BROADCAST_HZ = 5        # how often (per second) we push a WebSocket update

SUMO_CMD = [
    "sumo",
    "-n", NET_FILE,
    "-r", ROUTE_FILE,
    "--step-length", str(STEP_LENGTH),
    "--no-step-log",
    "--start",
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(simulation_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        if not self.active:
            return
        data = json.dumps(message)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def _get_tls_link_positions():
    """One marker position per controlled link (approach), not per junction.
    A single dot per junction is misleading: real intersections almost always
    have at least one approach with green while cross-traffic sits at red, so
    a "show green if ANY link is green" marker is basically always green.
    Instead we place a marker ~8m back along each incoming lane (before the
    stop line) so every approach gets its own, independently-colored dot --
    matching how a real signalized intersection actually looks.

    Returns: {tls_id: [(link_index, lon, lat), ...]}
    """
    result = {}
    for tls_id in traci.trafficlight.getIDList():
        try:
            link_groups = traci.trafficlight.getControlledLinks(tls_id)
        except traci.exceptions.TraCIException:
            continue

        positions = []
        for index, group in enumerate(link_groups):
            if not group:
                continue
            incoming_lane = group[0][0]
            try:
                shape = traci.lane.getShape(incoming_lane)
            except traci.exceptions.TraCIException:
                continue
            if len(shape) < 2:
                continue
            # Point ~8m back from the stop line (end of the lane shape),
            # interpolated along the final segment.
            (x1, y1), (x2, y2) = shape[-2], shape[-1]
            seg_len = math.hypot(x2 - x1, y2 - y1) or 1.0
            back = min(8.0, seg_len)
            t = 1.0 - (back / seg_len)
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            lon, lat = traci.simulation.convertGeo(x, y)
            positions.append((index, lon, lat))

        if positions:
            result[tls_id] = positions
    return result


def _link_color(state_char: str) -> str:
    """Map a single per-link state character to a display color.
    SUMO's link-state alphabet has more values than a 3-color light (e.g.
    'y'/'Y' minor-yield, 's' stop, 'u' red+yellow, 'o' off) -- collapsed
    here to the three colors a real signal head shows."""
    c = state_char.lower()
    if c == "g":
        return "green"
    if c == "y":
        return "yellow"
    return "red"


async def simulation_loop():
    """Runs forever: steps SUMO, broadcasts vehicle state, loops the route
    file when the simulation empties out so the demo runs continuously."""
    traci.start(SUMO_CMD)
    print("TraCI connected, simulation running.")

    tick_interval = 1.0 / BROADCAST_HZ
    steps_per_tick = max(1, round(tick_interval / STEP_LENGTH))
    tls_links = _get_tls_link_positions()
    total_links = sum(len(v) for v in tls_links.values())
    print(f"Tracking {len(tls_links)} traffic-light junctions, {total_links} approaches.")

    try:
        while True:
            try:
                for _ in range(steps_per_tick):
                    traci.simulationStep()

                vehicles = []
                for vid in traci.vehicle.getIDList():
                    try:
                        x, y = traci.vehicle.getPosition(vid)
                        lon, lat = traci.simulation.convertGeo(x, y)
                        vehicles.append({
                            "id": vid,
                            "lon": lon,
                            "lat": lat,
                            "angle": traci.vehicle.getAngle(vid),  # degrees, 0=N, clockwise
                            "speed": traci.vehicle.getSpeed(vid),  # m/s
                        })
                    except traci.exceptions.TraCIException:
                        # Vehicle can vanish between getIDList() and these calls
                        # (arrived/removed mid-step) -- skip it, not fatal.
                        continue

                traffic_lights = []
                for tls_id, links in tls_links.items():
                    try:
                        state = traci.trafficlight.getRedYellowGreenState(tls_id)
                    except traci.exceptions.TraCIException:
                        continue
                    for index, lon, lat in links:
                        if index >= len(state):
                            continue
                        traffic_lights.append({
                            "id": f"{tls_id}_{index}",
                            "lon": lon,
                            "lat": lat,
                            "color": _link_color(state[index]),
                        })

                await manager.broadcast({
                    "type": "tick",
                    "simTime": traci.simulation.getTime(),
                    "vehicleCount": len(vehicles),
                    "vehicles": vehicles,
                    "trafficLights": traffic_lights,
                })

                # If the route file has been fully consumed and no cars remain,
                # reload the simulation so the demo keeps running.
                if traci.simulation.getMinExpectedNumber() <= 0:
                    await manager.broadcast({"type": "reset"})
                    traci.load(SUMO_CMD[1:])  # reload same net/route, skip binary name
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let one bad tick silently kill the whole background
                # loop -- that leaves the server up (page/WebSocket still
                # connect fine) but frozen on "waiting for data" forever,
                # which is confusing to debug from the client side.
                print(f"simulation_loop tick error (continuing): {e!r}")

            await asyncio.sleep(tick_interval)
    except asyncio.CancelledError:
        pass
    finally:
        traci.close()
        print("TraCI closed.")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # we don't expect client messages, just keep-alive
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/")
async def index():
    return FileResponse("cesium_traffic.html")


@app.get("/{filename}")
async def static_file(filename: str):
    # Browsers auto-request /favicon.ico; we don't ship one. FileResponse
    # raises an unhandled RuntimeError for a missing path instead of a clean
    # 404, so guard it explicitly rather than 500ing (and spamming the log)
    # on every page load.
    if not os.path.isfile(filename):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
