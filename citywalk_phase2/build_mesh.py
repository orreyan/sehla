"""
Build a 3D building mesh (glTF/GLB) from a small OSM extract, for loading into CesiumJS.
This is a lightweight stand-in for OSM2World (which needs Java 17 + Maven, unavailable
in this sandbox) -- it extrudes building footprints to their tagged/estimated height.
"""
import math
import xml.etree.ElementTree as ET
import numpy as np
import trimesh
from shapely.geometry import Point, Polygon

OSM_FILE = "map.osm"
OUT_GLB = "citywalk_buildings.glb"
DEFAULT_LEVEL_HEIGHT = 3.2   # meters per building level, typical estimate
DEFAULT_HEIGHT = 8.0         # fallback if no height/levels tag at all

tree = ET.parse(OSM_FILE)
root = tree.getroot()

# bounds -> origin for local ENU (east-north-up) projection
bounds = root.find("bounds")
min_lat, min_lon = float(bounds.get("minlat")), float(bounds.get("minlon"))
max_lat, max_lon = float(bounds.get("maxlat")), float(bounds.get("maxlon"))
origin_lat = (min_lat + max_lat) / 2
origin_lon = (min_lon + max_lon) / 2

R_EARTH = 6378137.0  # WGS84 equatorial radius, meters

def latlon_to_local_xy(lat, lon):
    """Equirectangular approx -- fine for a ~1km-scale area.

    NOTE: returns (north, -east), not the "natural" (east, north). Determined
    empirically with isolated single-box test files loaded the same way as
    the real model: a box authored at local (x=100, y=0, z=up) rendered in
    Cesium at (east=0, north=+100); a box authored at (x=0, y=100, z=up)
    rendered at (east=-100, north=0). That means, end to end through our
    Z-up->Y-up export rotation plus however Cesium's model loader handles a
    glTF's Y-up convention, the effective mapping is world_east=-input_y,
    world_north=input_x -- a proper (non-reflecting) 90-degree rotation, not
    a plain axis swap. Solving for the input that makes world_east/north
    match true east/north gives (input_x, input_y) = (north, -east), which
    is what's returned here. Verified by picking real building footprints
    across the whole map after this change (see diagnosis notes for way
    495448021's corner, previously unpickable at its true position).
    """
    east = math.radians(lon - origin_lon) * R_EARTH * math.cos(math.radians(origin_lat))
    north = math.radians(lat - origin_lat) * R_EARTH
    return north, -east

# index all nodes
nodes = {}
for nd in root.findall("node"):
    nid = nd.get("id")
    lat, lon = float(nd.get("lat")), float(nd.get("lon"))
    nodes[nid] = latlon_to_local_xy(lat, lon)

def parse_height(tags):
    if "height" in tags:
        try:
            return float(tags["height"].replace("m", "").strip())
        except ValueError:
            pass
    if "building:levels" in tags:
        try:
            return float(tags["building:levels"]) * DEFAULT_LEVEL_HEIGHT
        except ValueError:
            pass
    return None  # no explicit height info -- caller decides the fallback

# --- Pass 1: parse every building/building:part way into a polygon ---------
# We keep lat/lon (for the bbox-crop check) and local xy (for extrusion and
# the building/building:part containment check) side by side.
candidates = []  # dicts: id, tags, poly_local, centroid_latlon, height (None if default)

for way in root.findall("way"):
    tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
    if "building" not in tags and "building:part" not in tags:
        continue

    refs = [nd.get("ref") for nd in way.findall("nd")]
    latlon_coords = []
    local_coords = []
    ok = True
    for r in refs:
        if r not in nodes:
            ok = False
            break
        local_coords.append(nodes[r])
    if not ok or len(local_coords) < 4 or local_coords[0] != local_coords[-1]:
        continue  # missing node or not a valid closed polygon

    try:
        poly_local = Polygon(local_coords)
        if not poly_local.is_valid or poly_local.area < 1.0:
            continue
    except Exception:
        continue

    cx, cy = poly_local.centroid.x, poly_local.centroid.y
    # local coords are (north, -east) -- see latlon_to_local_xy. Invert accordingly.
    centroid_lat = origin_lat + math.degrees(cx / R_EARTH)
    centroid_lon = origin_lon + math.degrees(-cy / (R_EARTH * math.cos(math.radians(origin_lat))))

    candidates.append({
        "tags": tags,
        "poly": poly_local,
        "centroid_lat": centroid_lat,
        "centroid_lon": centroid_lon,
        "height": parse_height(tags),
        "is_part": "building:part" in tags,
    })

# --- Fix 1: drop footprints whose centroid falls outside the fetched bbox --
# A way straddling the box edge still gets exported in full by the OSM API
# (it isn't clipped), but its real neighbors beyond the edge were never
# fetched. Rendering it anyway makes it look like an isolated building
# standing alone in open space -- keep the mesh limited to what's actually
# inside the area we exported.
in_bbox = []
edge_dropped = 0
for c in candidates:
    if min_lat <= c["centroid_lat"] <= max_lat and min_lon <= c["centroid_lon"] <= max_lon:
        in_bbox.append(c)
    else:
        edge_dropped += 1

# --- Fix 2: don't double-extrude building + building:part pairs ------------
# When a `building=yes` outer shell has no height/levels of its own, it's
# just a footprint container for one or more `building:part` ways that carry
# the real height. Extruding both independently stacks a DEFAULT_HEIGHT box
# underneath/around the real tower. If a building:part's centroid falls
# inside such a shell, skip the shell and keep only the part(s).
parts = [c for c in in_bbox if c["is_part"]]
shells_to_drop = set()
for i, c in enumerate(in_bbox):
    if c["is_part"] or c["height"] is not None:
        continue  # only applies to buildings with no explicit height
    for p in parts:
        px, py = p["poly"].centroid.x, p["poly"].centroid.y
        if c["poly"].contains(Point(px, py)):
            shells_to_drop.add(i)
            break

meshes = []
skipped = edge_dropped
merged_shells = len(shells_to_drop)

# Plain-white extrusions read as flat, undifferentiated blocks once the
# default OSM raster basemap is right there for contrast -- a soft warm
# gray (the same tone most 3D-city viewers use, e.g. Cesium OSM Buildings)
# reads as "building material" instead of "untextured placeholder".
BUILDING_COLOR = [214, 209, 196, 255]  # RGBA

for i, c in enumerate(in_bbox):
    if i in shells_to_drop:
        continue
    try:
        height = c["height"] if c["height"] is not None else DEFAULT_HEIGHT
        mesh = trimesh.creation.extrude_polygon(c["poly"], height=height)
        # vertex_colors (not face_colors) -- avoids needing scipy for the
        # face->vertex conversion trimesh's GLB exporter otherwise triggers.
        mesh.visual.vertex_colors = np.tile(BUILDING_COLOR, (len(mesh.vertices), 1))
        meshes.append(mesh)
    except Exception:
        skipped += 1
        continue

print(f"Built {len(meshes)} building meshes")
print(f"  skipped (invalid/missing nodes): {skipped - edge_dropped}")
print(f"  dropped (centroid outside fetched bbox): {edge_dropped}")
print(f"  merged (building shell absorbed by building:part): {merged_shells}")

scene = trimesh.Scene(meshes)

# Our mesh was authored Z-up (height along Z, matching the local ENU frame we'll
# anchor it to in Cesium). glTF/GLB requires Y-up, and trimesh does not convert
# this automatically -- without it, Cesium ends up rendering buildings on their
# side. Rotate -90 deg about X: (x, y, z) -> (x, z, -y).
#
# This is a proper rotation (determinant +1), not a reflection, so it doesn't
# disturb face winding/normals -- the east/north correction is handled earlier
# in latlon_to_local_xy instead (a relabeling of which local axis is which,
# not a mirroring of exported geometry), keeping this export step exactly the
# already-verified fix for the vertical (buildings-on-their-side) bug.
Z_UP_TO_Y_UP = np.array([
    [1,  0, 0, 0],
    [0,  0, 1, 0],
    [0, -1, 0, 0],
    [0,  0, 0, 1],
], dtype=float)
scene.apply_transform(Z_UP_TO_Y_UP)

scene.export(OUT_GLB)
print(f"Wrote {OUT_GLB}")
print(f"Origin (for Cesium anchor): lat={origin_lat}, lon={origin_lon}")
