#!/usr/bin/env python3
"""
Fills in everything the field map needs. Run it once:

    python3 fetch.py

Covers two objectives only, sunstone and placer gold, in 15 small zones
(see ZONES below) instead of the whole state. Each zone is a box about
5 km out from every target point in it; where two boxes touched they
were merged, so no ground is fetched twice.

Inside each zone, three layers, all unfiltered:

  1. DOGAMI MILO release 4, every commodity - gold, silver and other
     metals as well as gems. Placer ground needs the gold records and
     the mercury and silver around them.

  2. BLM mining claims, every open case in the zone. There is no commodity
     filter and there cannot be one: BLM records no commodity for a claim.

  3. DOGAMI OGDC-6 geology, every map unit, cut to the zone edge. The old
     statewide file kept only the eleven units that host agate and opal,
     which left the Picture Gorge basalts out of every sunstone zone.

Outside the zones there is no claim, MILO or geology data at all. Your
field sites file (data/rockhounding.geojson) is not touched by this script
and stays statewide.

To check the sources are alive without downloading anything:

    python3 fetch.py --check

Needs Python 3.8 or newer. No extra packages.
"""

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------
# SETTINGS you might want to change.
# ----------------------------------------------------------------------

# Include gold and other metals. On: half of this map is placer gold.
GOLD = True

# The ground to pull. Each zone is (objective, name, west, south, east,
# north). Boxes run about 5 km out from each target point; points whose
# boxes touched were merged into one zone, and the merge was repeated
# until no two zones overlap.
#
# Only boxes are stored here, never the points themselves - this file can
# sit in a public repo without giving away where the targets are.
#
# To add a zone, add a line. To drop one, delete its line and re-run.
ZONES = [
    ("placer",   "Upper Big Creek",             (-118.848, 44.745, -118.722, 44.835)),
    ("placer",   "Ochoco Creek",                (-120.503, 44.335, -120.269, 44.485)),
    ("placer",   "Dixie / Quartzburg",          (-118.763, 44.465, -118.507, 44.643)),
    ("placer",   "Fox / Mine Creek",            (-119.231, 44.500, -119.097, 44.623)),
    ("placer",   "Elk Creek",                   (-118.863, 44.647, -118.737, 44.737)),
    ("placer",   "Spanish Gulch",               (-119.833, 44.400, -119.621, 44.580)),
    ("sunstone", "Silvies / West Myrtle Butte", (-119.233, 43.943, -119.014, 44.144)),
    ("sunstone", "NE Oregon",                   (-117.650, 45.108, -117.522, 45.199)),
    ("sunstone", "Silvies west",                (-119.883, 44.155, -119.717, 44.275)),
    ("sunstone", "Warner",                      (-120.073, 42.623, -119.945, 42.721)),
    ("sunstone", "Harney",                      (-120.004, 43.086, -119.880, 43.176)),
    ("sunstone", "Ponderosa",                   (-119.588, 43.811, -119.364, 43.964)),
    ("sunstone", "Silvies east",                (-118.905, 43.868, -118.781, 43.959)),
    ("sunstone", "Dust Devil",                  (-119.927, 42.669, -119.805, 42.759)),
    ("sunstone", "Plush",                       (-119.957, 42.779, -119.834, 42.870)),
]

# ----------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VENDOR = os.path.join(HERE, "vendor")

MAPLIBRE = "4.7.1"
VENDOR_FILES = [
    (f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.js",
     "maplibre-gl.js"),
    (f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.css",
     "maplibre-gl.css"),
]

# MILO release 4 (24,664 records statewide, and it carries DOGAMI's own
# assay results). Release 3 lived at Public/MILOv3/MapServer/1.
MILO_URL = ("https://gis.dogami.oregon.gov/arcgis/rest/services/"
            "Public/MILO/MapServer/1/query")
CLAIMS_URL = ("https://gis.blm.gov/nlsdb/rest/services/HUB/"
              "BLM_Natl_MLRS_Mining_Claims_Not_Closed/FeatureServer/0/query")
GEOLOGY_URL = ("https://gis.dogami.oregon.gov/arcgis/rest/services/"
               "Public/OGDC6/MapServer/2/query")

# Every map unit in each zone is kept. The statewide map kept only the
# eleven units MILO showed hosting agate, thundereggs and opal; that list
# held no basalt the sunstone comes from and none of the gravels and
# intrusives placer gold comes from. Inside a few small zones there is no
# size reason to filter, so nothing is.
GEOLOGY_KEEP = ["MAP_UNIT_N", "MAP_UNIT_L", "AGE_NAME", "G_ROCK_TYP",
                "LITH_GEN_U", "TERRANE_GR", "FORMATION", "MEMBER",
                "Citation", "Link"]

# MILO v3 misspelled this field as "CommodityAbreviation", one b. Release 4
# corrected it to "CommodityAbbreviation". Both are listed so the file works
# against either release. This is the field that carries the actual mineral -
# Commodity only ever says "gemstone material" - so losing it collapses every
# gem into one category and hides the sunstone records entirely.
MILO_KEEP = ["SiteName", "Commodity", "CommodityAbbreviation",
             "CommodityAbreviation", "CommoditiesProduced",
             "Type", "DepositType", "OreMaterial", "WorkingsType",
             "WorkingsDescription", "YearOfDiscovery", "ElevationFeet", "Owner",
             "County", "Township", "Range", "Section", "TopoMap24k", "TopoMap100k",
             "MapUnit", "MapUnitName", "ThematicLithology", "ThematicAge",
             "ThematicFormation", "ThematicTerraneGroup",
             "ShortReference1", "ShortReference2", "ShortReference3", "MILO_ID"]

# QLTY is a diagnostic string the server tacks on. It is pure noise and it
# was a quarter of the old claims file, so it is not in this list.
CLAIMS_KEEP = ["OBJECTID", "CSE_NAME", "CSE_NR", "CSE_TYPE_NR", "CSE_DISP",
               "RCRD_ACRS", "LEG_CSE_NR"]

# How claims are stored. BLM records a claim only to the quarter sections
# it affects - the polygons it hands back are survey-grid rectangles, not
# staked corners. Drawing each one implies a precision the data does not
# have, and costs about 10 MB on the phone.
#
# So claims are aggregated into cells: one square per patch of ground,
# carrying how many claims sit on it, what kinds, and how big they are.
# That is the same thing BLM actually knows, at about a twentieth the size.
#
# CLAIM_CELL is the cell edge in degrees. 0.01 is close to a quarter
# section (about half a mile). Use 0.02 for section-sized cells and a
# smaller file, or 0.005 for finer ground and a bigger one.
CLAIM_CELL = 0.01

# Also write the full claim polygons to data/claims-detail.geojson, with
# each claim's name, serial number and type. Statewide this was the 10 MB
# file; inside the zones it is small, so it is on. The app does not read
# it yet - claims.geojson (cells) keeps the current app working until the
# app is updated to draw real outlines.
CLAIM_DETAIL = True

PRECISION = 5   # about a metre - finer than the survey grid this comes from

# ----------------------------------------------------------------------
# Same classifier as the Rabbit Basin map, so the two agree about what
# counts as what. First match wins.
# ----------------------------------------------------------------------
COMMODITY_RULES = [
    ("gold",      ["gold", "placer gold", "au "]),
    ("silver",    ["silver", "argent"]),
    ("sunstone",  ["sunstone", "sun stone", "labradorite", "feldspar gem"]),
    ("opal",      ["opal"]),
    # Agate, jasper, carnelian and chalcedony used to be one rule, which
    # meant 132 jasper records came out labelled "agate" and no jasper
    # category existed to switch on. They are separate now.
    #
    # Order matters - first match wins. Carnelian and chalcedony go first
    # because a record reading "carnelian agate" is a carnelian record,
    # and one reading "chalcedony" should not be swallowed by "agate".
    # Bloodstone before jasper for the same reason: it is a jasper, but
    # it is the one people drive for.
    ("bloodstone", ["bloodstone", "blood stone", "heliotrope"]),
    ("carnelian", ["carnelian", "cornelian", "sard"]),
    ("chalcedony", ["chalcedony", "chrysoprase"]),
    ("agate",     ["agate", "moss agate", "plume agate"]),
    ("jasper",    ["jasper", "jaspe"]),
    ("wood",      ["petrified wood", "fossil wood", "silicified wood", "petrified"]),
    ("thunderegg", ["thunderegg", "thunder egg", "geode"]),
    ("amethyst",  ["amethyst"]),
    ("jade",      ["jade", "nephrite"]),
    ("zeolite",   ["zeolite"]),
    ("obsidian",  ["obsidian"]),
    ("gem_other", ["gem material", "gemstone", "gem", "garnet",
                   "rhodonite", "serpentine", "turquoise", "variscite", "onyx",
                   "beryl", "topaz", "sapphire", "ruby", "peridot", "olivine gem",
                   "quartz crystal", "rock crystal", "crystal"]),
    ("metal",     ["copper", "lead", "zinc", "mercury", "cinnabar", "chromite",
                   "chromium", "nickel", "cobalt", "manganese", "antimony",
                   "tungsten", "uranium", "platinum", "molybdenum", "tin",
                   "titanium", "arsenic", "iron", "bismuth", "vanadium",
                   "rare earth", "thorium", "beryllium", "lithium"]),
]

GEM_CATS = {"sunstone", "opal", "agate", "jasper", "carnelian", "chalcedony",
            "bloodstone", "amethyst", "jade", "zeolite", "wood", "thunderegg",
            "obsidian", "gem_other"}

CTX = ssl.create_default_context()
UA = {"User-Agent": "oregon-rockhound-map/1.0 (personal offline map)"}


def get(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def classify(props):
    """Return (category, group), or (None, None) to throw the record away.

    SiteName is deliberately not read: mines get named things like
    "Gold Sheen" that say nothing about what is in them.
    """
    text = " ".join(str(props.get(f) or "") for f in
                    ("CommodityAbbreviation", "CommodityAbreviation", "Commodity",
                     "CommoditiesProduced", "OreMaterial")).lower()
    if not text.strip():
        return None, None
    for cat, words in COMMODITY_RULES:
        for w in words:
            if w in text:
                return cat, ("gem" if cat in GEM_CATS else "metal")
    return None, None


def trim_coords(node):
    if isinstance(node, list):
        if node and isinstance(node[0], (int, float)):
            return [round(v, PRECISION) for v in node]
        return [trim_coords(v) for v in node]
    return node


def slim(feature, keep):
    props = feature.get("properties") or {}
    props = {k: v for k, v in props.items()
             if k in keep and v not in (None, "", "Null", " ")}
    geom = feature.get("geometry")
    if geom and "coordinates" in geom:
        geom = dict(geom)
        geom["coordinates"] = trim_coords(geom["coordinates"])
    return {"type": "Feature", "properties": props, "geometry": geom}


def query(url, bbox, page=1000, label="", where="1=1", order="OBJECTID"):
    """Pull every feature inside bbox, one page at a time."""
    features = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "f": "geojson",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page,
            # Paging with resultOffset is only stable if the server sorts
            # rows the same way every request. With no explicit order the
            # sort is undefined, so pages can overlap and records fall
            # between them - they simply never arrive, with no error.
            "orderByFields": order,
            "geometry": ",".join(str(v) for v in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
        }
        doc = json.loads(get(url + "?" + urllib.parse.urlencode(params)))
        if "error" in doc:
            raise RuntimeError(doc["error"].get("message", "server error"))
        batch = doc.get("features") or []
        features.extend(batch)
        if label:
            sys.stdout.write(f"\r    {label} {len(features):,} features…")
            sys.stdout.flush()
        # Advance by what actually came back, not by what we asked for. The
        # server may cap the page size well below `page`; trusting our own
        # number here silently loses every record past the first page.
        if not batch:
            break
        offset += len(batch)
        if offset > 300000:
            break
    if label:
        sys.stdout.write("\r" + " " * 50 + "\r")
    return features



def claim_bbox(geom):
    """West, south, east, north of a polygon or multipolygon."""
    b = [180.0, 90.0, -180.0, -90.0]

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            if c[0] < b[0]: b[0] = c[0]
            if c[1] < b[1]: b[1] = c[1]
            if c[0] > b[2]: b[2] = c[0]
            if c[1] > b[3]: b[3] = c[1]
        else:
            for v in c:
                walk(v)

    if not geom or "coordinates" not in geom:
        return None
    walk(geom["coordinates"])
    return b if b[0] <= b[2] else None


def clip_ring(ring, box):
    """Cut one polygon ring to a rectangle (Sutherland-Hodgman)."""
    w, s, e, n = box
    edges = ((lambda p: p[0] >= w, lambda a, b: _at_x(a, b, w)),
             (lambda p: p[0] <= e, lambda a, b: _at_x(a, b, e)),
             (lambda p: p[1] >= s, lambda a, b: _at_y(a, b, s)),
             (lambda p: p[1] <= n, lambda a, b: _at_y(a, b, n)))
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring[:]
    for inside, cross in edges:
        if not pts:
            break
        out = []
        prev = pts[-1]
        for cur in pts:
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
            prev = cur
        pts = out
    if len(pts) < 3:
        return None
    return pts + [pts[0]]


def _at_x(a, b, x):
    t = (x - a[0]) / (b[0] - a[0])
    return [x, a[1] + t * (b[1] - a[1])]


def _at_y(a, b, y):
    t = (y - a[1]) / (b[1] - a[1])
    return [a[0] + t * (b[0] - a[0]), y]


def clip_geom(geom, box):
    """Cut a Polygon or MultiPolygon to a rectangle. None if nothing left.

    Geology polygons can run a hundred kilometres past a zone. Cutting
    them at the zone edge is what keeps the file small, and it is what
    makes 'no geology outside the zones' literally true.
    """
    if not geom:
        return None
    t = geom.get("type")
    polys = ([geom["coordinates"]] if t == "Polygon"
             else geom["coordinates"] if t == "MultiPolygon" else [])
    kept = []
    for poly in polys:
        if not poly:
            continue
        outer = clip_ring(poly[0], box)
        if not outer:
            continue
        holes = [h for h in (clip_ring(r, box) for r in poly[1:]) if h]
        kept.append([outer] + holes)
    if not kept:
        return None
    if len(kept) == 1:
        return {"type": "Polygon", "coordinates": kept[0]}
    return {"type": "MultiPolygon", "coordinates": kept}


def aggregate_claims(features, cell):
    """Collapse claim polygons into a grid of cells.

    One cell per patch of ground that carries at least one claim. Each
    cell reports how many claims touch it, the split by kind, and the
    acreage range - which is what you actually want to know standing on
    the ground. Individual claim names and serials are dropped; look
    those up in MLRS if you ever need one.

    A claim spanning more than one cell is counted in each cell it
    touches, so counts across cells sum to more than the claim total.
    That is deliberate: the question a cell answers is "what is on THIS
    ground", not "how many claims exist".
    """
    kinds = {"384101": "lode", "384103": "lode",
             "384201": "placer", "384203": "placer",
             "384301": "mill", "384303": "mill",
             "384401": "tunnel", "384403": "tunnel"}
    cells = {}
    spanning = 0

    for f in features:
        b = claim_bbox(f.get("geometry"))
        if not b:
            continue
        p = f.get("properties") or {}
        kind = kinds.get(str(p.get("CSE_TYPE_NR") or ""), "other")
        try:
            acres = float(p.get("RCRD_ACRS") or 0) or None
        except (TypeError, ValueError):
            acres = None

        x0, y0 = int(b[0] // cell), int(b[1] // cell)
        x1, y1 = int(b[2] // cell), int(b[3] // cell)
        if (x1 - x0) or (y1 - y0):
            spanning += 1
        # Cap the span so one bad geometry cannot carpet the state.
        if (x1 - x0) > 40 or (y1 - y0) > 40:
            x1, y1 = x0, y0

        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                c = cells.setdefault((gx, gy), {"n": 0, "acres": [],
                                                "lode": 0, "placer": 0,
                                                "mill": 0, "tunnel": 0,
                                                "other": 0})
                c["n"] += 1
                c[kind] += 1
                if acres:
                    c["acres"].append(acres)

    out = []
    for (gx, gy), c in cells.items():
        w, s = gx * cell, gy * cell
        e, n = w + cell, s + cell
        props = {"n": c["n"]}
        for k in ("lode", "placer", "mill", "tunnel", "other"):
            if c[k]:
                props[k] = c[k]
        if c["acres"]:
            a = sorted(c["acres"])
            props["acres_min"] = round(a[0], 1)
            props["acres_max"] = round(a[-1], 1)
            props["acres_mid"] = round(a[len(a) // 2], 1)
        out.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [[
                [round(w, PRECISION), round(s, PRECISION)],
                [round(e, PRECISION), round(s, PRECISION)],
                [round(e, PRECISION), round(n, PRECISION)],
                [round(w, PRECISION), round(n, PRECISION)],
                [round(w, PRECISION), round(s, PRECISION)]]]}
        })
    return out, spanning


def check():
    print("Checking sources.\n")
    bad = 0
    for name, url in (("Mineral occurrences", MILO_URL),
                      ("Mining claims", CLAIMS_URL),
                      ("Geology", GEOLOGY_URL)):
        base = url.rsplit("/query", 1)[0]
        try:
            doc = json.loads(get(base + "?f=json", timeout=30))
            print(f"  OK    {name:<22} {doc.get('name') or 'ok'}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {name:<22} {exc}")
            print(f"        {base}")
    print()
    if bad:
        print(f"{bad} source(s) unreachable. If it is not your connection, the")
        print("agency moved the service — find the new address in their REST")
        print("directory and update the url near the top of this file.")
    else:
        print("All three sources are live.")
    return 1 if bad else 0


def vendor():
    os.makedirs(VENDOR, exist_ok=True)
    for url, name in VENDOR_FILES:
        dest = os.path.join(VENDOR, name)
        if os.path.exists(dest):
            print(f"  have  {name}")
            continue
        print(f"  get   {name}")
        with open(dest, "wb") as fh:
            fh.write(get(url))


def fetch_zones(url, page, label, where="1=1"):
    """Query every zone. Returns a list of (zone index, raw features)."""
    out = []
    for i, (kind, name, box) in enumerate(ZONES):
        out.append((i, query(url, box, page, f"{label} {name}:", where=where)))
    return out


def fid(f, *fields):
    """A stable identity for de-duplicating features that sit in two zones."""
    if f.get("id") is not None:
        return ("id", f["id"])
    p = f.get("properties") or {}
    return tuple(p.get(k) for k in fields) or None


def main():
    if "--check" in sys.argv:
        return check()

    os.makedirs(DATA, exist_ok=True)
    print(f"{len(ZONES)} zones: "
          f"{sum(z[0] == 'placer' for z in ZONES)} placer, "
          f"{sum(z[0] == 'sunstone' for z in ZONES)} sunstone")
    print("Occurrences, claims and geology: everything inside the zones, "
          "nothing outside.\n")

    print("Map library")
    vendor()

    per_zone = [{"milo": 0, "claims": 0, "geology": 0} for _ in ZONES]

    # ---- occurrences -------------------------------------------------
    print("\nMineral occurrences")
    print("  Oregon DOGAMI, Mineral Information Layer (MILO), every commodity")
    try:
        batches = fetch_zones(MILO_URL, 1000, "MILO")
    except Exception as exc:
        print(f"    could not download: {exc}")
        print("    Run  python3 fetch.py --check  to see if the source is down.")
        return 1

    wanted = set(GEM_CATS) | ({"gold", "silver", "metal"} if GOLD else set())
    kept, seen_ids, raw_all, unclassified = [], set(), [], 0
    dropped = {}
    for i, raw in batches:
        raw_all.extend(raw)
        for f in raw:
            key = fid(f, "MILO_ID", "SiteName")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            f = slim(f, set(MILO_KEEP))
            cat, grp = classify(f["properties"])
            if cat not in wanted:
                unclassified += 1
                p = f["properties"]
                what = " / ".join(str(p.get(k)) for k in
                                  ("CommodityAbbreviation", "Commodity")
                                  if p.get(k)) or "(no commodity recorded)"
                dropped[what] = dropped.get(what, 0) + 1
                continue
            f["properties"]["_cat"] = cat
            f["properties"]["_grp"] = grp
            kept.append(f)
            per_zone[i]["milo"] += 1

    # Name every field in MILO_KEEP that never came back. A renamed field
    # shows up here by name instead of vanishing in silence - that is how
    # CommodityAbreviation -> CommodityAbbreviation was caught.
    seen = set()
    for f in raw_all:
        seen.update((f.get("properties") or {}).keys())
    absent = [f for f in MILO_KEEP if f not in seen]
    if absent:
        print(f"    NOTE: {len(absent)} field(s) in MILO_KEEP never appeared:")
        print("      " + ", ".join(absent))
        print("    Either no record in the zones fills them, or MILO renamed")
        print("    them. Check the current names at:")
        print("    " + MILO_URL.replace("/query", "?f=pjson"))

    if kept and not any("CommodityAbbreviation" in f["properties"]
                        or "CommodityAbreviation" in f["properties"]
                        for f in kept):
        print("    WARNING: no record carries a commodity abbreviation, so the")
        print("    categories below are meaningless. Fix before shipping.")

    path = os.path.join(DATA, "milo.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": kept}, fh)
    print(f"    {len(seen_ids):,} records in zones -> {len(kept):,} kept, "
          f"{os.path.getsize(path)/1048576:.2f} MB")
    if unclassified:
        print(f"    {unclassified:,} had no commodity the classifier knows "
              "and were left out. What they say:")
        for w in sorted(dropped, key=lambda k: -dropped[k])[:15]:
            print(f"      {dropped[w]:>4}  {w[:70]}")
        if len(dropped) > 15:
            print(f"      ... and {len(dropped) - 15} more kinds")

    tally = {}
    for f in kept:
        c = f["properties"]["_cat"]
        tally[c] = tally.get(c, 0) + 1
    for c in sorted(tally, key=lambda k: -tally[k]):
        print(f"      {c:<12} {tally[c]:,}")

    # ---- claims: everything in the zones ----------------------------
    print("\nMining claims")
    print("  BLM Mineral and Land Records System, cases not closed")
    try:
        batches = fetch_zones(CLAIMS_URL, 1000, "claims")
    except Exception as exc:
        print(f"    could not download: {exc}")
        return 1
    claims, seen_ids = [], set()
    for i, raw in batches:
        for f in raw:
            per_zone[i]["claims"] += 1
            key = fid(f, "OBJECTID", "CSE_NR")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            claims.append(slim(f, set(CLAIMS_KEEP)))

    cells, spanning = aggregate_claims(claims, CLAIM_CELL)
    path = os.path.join(DATA, "claims.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": cells}, fh)
    print(f"    {len(claims):,} claims -> {len(cells):,} cells, "
          f"{os.path.getsize(path)/1048576:.2f} MB")
    if spanning:
        print(f"      {spanning:,} claims span more than one cell and are "
              "counted in each")

    if CLAIM_DETAIL:
        dpath = os.path.join(DATA, "claims-detail.geojson")
        with open(dpath, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": claims}, fh)
        print(f"    full outlines -> claims-detail.geojson, "
              f"{os.path.getsize(dpath)/1048576:.2f} MB")

    # ---- geology: every unit, cut to the zone ------------------------
    print("\nGeology")
    print("  Oregon DOGAMI, OGDC-6, every map unit, cut at the zone edge")
    try:
        batches = fetch_zones(GEOLOGY_URL, 400, "geology")
    except Exception as exc:
        print(f"    could not download: {exc}")
        print("    The map works without it - the geology layer will just")
        print("    read as not downloaded.")
        batches = []

    geo = []
    for i, raw in batches:
        box = ZONES[i][2]
        for f in raw:
            f = slim(f, set(GEOLOGY_KEEP))
            g = clip_geom(f.get("geometry"), box)
            if not g:
                continue
            f["geometry"] = trim_coords_geom(g)
            geo.append(f)
            per_zone[i]["geology"] += 1

    if geo:
        path = os.path.join(DATA, "geology.geojson")
        with open(path, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": geo}, fh)
        print(f"    {len(geo):,} polygon pieces, "
              f"{os.path.getsize(path)/1048576:.2f} MB")
        units = {}
        for f in geo:
            u = f["properties"].get("MAP_UNIT_N") or "?"
            units[u] = units.get(u, 0) + 1
        print(f"    {len(units)} different map units. Most common:")
        for u in sorted(units, key=lambda k: -units[k])[:12]:
            print(f"      {units[u]:>5,}  {u}")

    # ---- per zone ----------------------------------------------------
    print("\nBy zone")
    print(f"    {'':<9} {'zone':<28} {'MILO':>5} {'claims':>7} {'geology':>8}")
    empty = []
    for (kind, name, _), c in zip(ZONES, per_zone):
        print(f"    {kind:<9} {name:<28} {c['milo']:>5} {c['claims']:>7} "
              f"{c['geology']:>8}")
        if not c["geology"]:
            empty.append(name)
    if empty:
        print("    No geology came back for: " + ", ".join(empty))
        print("    Every square metre of Oregon is mapped, so that means the")
        print("    query failed for those zones, not that the ground is blank.")

    # The app draws the zone outlines and lists them from this file, so the
    # zones are defined once, here, and nowhere else.
    zf = []
    for (kind, name, (w, s, e, n)), c in zip(ZONES, per_zone):
        zf.append({"type": "Feature",
                   "properties": {"name": name, "kind": kind,
                                  "milo": c["milo"], "claims": c["claims"],
                                  "geology": c["geology"]},
                   "geometry": {"type": "Polygon", "coordinates": [[
                       [w, s], [e, s], [e, n], [w, n], [w, s]]]}})
    with open(os.path.join(DATA, "zones.geojson"), "w") as fh:
        json.dump({"type": "FeatureCollection", "features": zf}, fh)

    total = sum(os.path.getsize(os.path.join(DATA, f)) for f in os.listdir(DATA))
    print(f"\nData total: {total/1048576:.1f} MB")
    print("Done. Open index.html.")
    return 0


def trim_coords_geom(g):
    g = dict(g)
    g["coordinates"] = trim_coords(g["coordinates"])
    return g


if __name__ == "__main__":
    sys.exit(main())
