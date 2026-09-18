#!/usr/bin/env python3
"""
Fills in everything the Oregon map needs. Run it once:

    python3 fetch.py

Two layers only, statewide:

  1. Gem and rockhounding occurrences from DOGAMI MILO, whole state.
  2. BLM mining claims, but ONLY the ones within 5 km of one of those
     occurrences. Statewide claims would be 10-30 MB and most of it is
     gold country you have no reason to look at. This keeps the file
     small and keeps every claim that could actually matter to you.

To check the sources are alive without downloading anything:

    python3 fetch.py --check

To add gold claims and mines later, set GOLD = True below and run it
again. The map already knows how to draw them.

Needs Python 3.8 or newer. No extra packages.
"""

import json
import math
import os
import ssl
import sys
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------
# SETTINGS you might want to change.
# ----------------------------------------------------------------------

# Include gold and other metals? Off for now. Flip to True and re-run to
# add them; nothing else needs changing.
GOLD = False

# How far from one of your anchor points a claim has to be before it is
# dropped. 15 km is about 9 miles.
CLAIM_RADIUS_KM = 15.0

# The ground you care about. Claims are pulled around THESE points, not
# around MILO occurrences.
#
# Anchoring on occurrences was a mistake: it threw away exactly the claims
# worth seeing. An active claim with no occurrence record means somebody
# staked ground, is paying to hold it, and nothing in any database says
# why. Those are the interesting ones.
#
# Add a line for any new ground. Claims are taken whole here - BLM records
# no commodity, so there is no way to ask for gem claims only. Keeping the
# anchors on gem country is what keeps the gold districts out.
ANCHORS = [
    ( 43.99540,  -119.15870),   # Silvies 1
    ( 45.15340,  -117.58600),   # NE Oregon 2
    ( 44.20000,  -119.78000),   # Silvies 3
    ( 44.23000,  -119.78000),   # Silvies 4
    ( 44.20000,  -119.82000),   # Silvies 5
    ( 42.67550,  -120.01200),   # Warner 6
    ( 42.66790,  -120.00600),   # Warner 7
    ( 43.13100,  -119.94200),   # Harney 8
    ( 45.88333,  -116.85000),   # NE Oregon X1
    ( 45.39460,  -117.82200),   # NE Oregon X2
    ( 45.27800,  -117.83300),   # NE Oregon X3
    ( 45.15600,  -117.77300),   # NE Oregon X4
    ( 45.50820,  -117.95300),   # NE Oregon X5
    ( 43.85650,  -119.52600),   # Ponderosa (ref)
    ( 42.71420,  -119.86620),   # Dust Devil (ref)
    ( 42.82440,  -119.89530),   # Plush (ref)
]

# The whole state, as west, south, east, north.
OREGON = (-124.70, 41.90, -116.40, 46.30)

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

MILO_KEEP = ["SiteName", "Commodity", "CommodityAbreviation", "CommoditiesProduced",
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

PRECISION = 6   # about four inches on the ground

# ----------------------------------------------------------------------
# Same classifier as the Rabbit Basin map, so the two agree about what
# counts as what. First match wins.
# ----------------------------------------------------------------------
COMMODITY_RULES = [
    ("gold",      ["gold", "placer gold", "au "]),
    ("silver",    ["silver", "argent"]),
    ("sunstone",  ["sunstone", "sun stone", "labradorite", "feldspar gem"]),
    ("opal",      ["opal"]),
    ("agate",     ["agate", "jasper", "chalcedony", "carnelian", "bloodstone",
                   "moss agate", "plume agate"]),
    ("wood",      ["petrified wood", "fossil wood", "silicified wood", "petrified"]),
    ("thunderegg", ["thunderegg", "thunder egg", "geode", "amethyst", "quartz crystal",
                    "rock crystal", "crystal"]),
    ("obsidian",  ["obsidian"]),
    ("gem_other", ["gem material", "gemstone", "gem", "jade", "nephrite", "garnet",
                   "rhodonite", "serpentine", "turquoise", "variscite", "onyx",
                   "beryl", "topaz", "sapphire", "ruby", "peridot", "olivine gem",
                   "zeolite gem"]),
    ("metal",     ["copper", "lead", "zinc", "mercury", "cinnabar", "chromite",
                   "chromium", "nickel", "cobalt", "manganese", "antimony",
                   "tungsten", "uranium", "platinum", "molybdenum", "tin",
                   "titanium", "arsenic", "iron", "bismuth", "vanadium",
                   "rare earth", "thorium", "beryllium", "lithium"]),
]

GEM_CATS = {"sunstone", "opal", "agate", "wood", "thunderegg", "obsidian", "gem_other"}

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
                    ("CommodityAbreviation", "Commodity",
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


def query(url, bbox, page=1000, label=""):
    """Pull every feature inside bbox, one page at a time."""
    features = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page,
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
        if len(batch) < page:
            break
        offset += page
        if offset > 300000:
            break
    if label:
        sys.stdout.write("\r" + " " * 50 + "\r")
    return features


def centroid(geom):
    """Rough centre of any geometry. Good enough for a distance screen."""
    xs, ys = [], []

    def walk(node):
        if isinstance(node, list):
            if node and isinstance(node[0], (int, float)):
                xs.append(node[0])
                ys.append(node[1])
            else:
                for v in node:
                    walk(v)
    walk((geom or {}).get("coordinates"))
    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def km_apart(lon1, lat1, lon2, lat2):
    R = 6371.0
    p = math.radians
    a = (math.sin(p(lat2 - lat1) / 2) ** 2 +
         math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(p(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def cells_around(points, radius_km, cell_deg=0.25):
    """Group occurrence points into a handful of boxes to query.

    One query per occurrence would be hundreds of round trips. Snapping
    them to a coarse grid first cuts that to a few dozen.
    """
    buckets = {}
    for lon, lat in points:
        key = (round(lon / cell_deg), round(lat / cell_deg))
        buckets.setdefault(key, []).append((lon, lat))
    boxes = []
    for pts in buckets.values():
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        mid_lat = sum(lats) / len(lats)
        dlat = radius_km / 111.32
        dlon = radius_km / (111.32 * max(math.cos(math.radians(mid_lat)), 0.1))
        boxes.append((min(lons) - dlon, min(lats) - dlat,
                      max(lons) + dlon, max(lats) + dlat))
    return boxes


def check():
    print("Checking sources.\n")
    bad = 0
    for name, url in (("Mineral occurrences", MILO_URL), ("Mining claims", CLAIMS_URL)):
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
        print("Both sources are live.")
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


def main():
    if "--check" in sys.argv:
        return check()

    os.makedirs(DATA, exist_ok=True)
    print("Oregon, whole state.")
    print(f"Occurrences: gem categories{' plus gold and metals' if GOLD else ' only'}, statewide.")
    print(f"Claims: everything within {CLAIM_RADIUS_KM:g} km of your "
          f"{len(ANCHORS)} anchor points.\n")

    print("Map library")
    vendor()

    # ---- occurrences -------------------------------------------------
    print("\nMineral occurrences")
    print("  Oregon DOGAMI, Mineral Information Layer (MILO)")
    try:
        raw = query(MILO_URL, OREGON, 1000, "MILO")
    except Exception as exc:
        print(f"    could not download: {exc}")
        print("    Run  python3 fetch.py --check  to see if the source is down.")
        return 1

    wanted = set(GEM_CATS) | ({"gold", "silver", "metal"} if GOLD else set())
    kept = []
    for f in raw:
        f = slim(f, set(MILO_KEEP))
        cat, grp = classify(f["properties"])
        if cat not in wanted:
            continue
        f["properties"]["_cat"] = cat
        f["properties"]["_grp"] = grp
        kept.append(f)

    # If MILO-4 renamed its fields, the allow-list above would quietly throw
    # everything away. Say so rather than writing a file full of empty records.
    if kept:
        avg = sum(len(f["properties"]) for f in kept) / len(kept)
        if avg < 4:
            print("    WARNING: records are coming through nearly empty, so the")
            print("    field names in MILO_KEEP probably changed in release 4.")
            print("    Open the service in a browser to see the current names:")
            print("    " + MILO_URL.replace("/query", "?f=pjson"))

    path = os.path.join(DATA, "milo.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": kept}, fh)
    print(f"    {len(raw):,} statewide records -> {len(kept):,} kept, "
          f"{os.path.getsize(path)/1048576:.1f} MB")

    tally = {}
    for f in kept:
        c = f["properties"]["_cat"]
        tally[c] = tally.get(c, 0) + 1
    for c in sorted(tally, key=lambda k: -tally[k]):
        print(f"      {c:<12} {tally[c]:,}")

    # ---- claims around YOUR anchors, not around the occurrences -------
    anchors = [(lon, lat) for lat, lon in ANCHORS]
    boxes = cells_around(anchors, CLAIM_RADIUS_KM)

    print(f"\nMining claims")
    print(f"  BLM Mineral and Land Records System, cases not closed")
    print(f"  Every claim within {CLAIM_RADIUS_KM:g} km of your {len(anchors)} "
          f"anchor points, whatever it is staked for.")
    print(f"  Those group into {len(boxes)} areas to ask about.")

    seen = {}
    failed = 0
    for n, box in enumerate(boxes, 1):
        sys.stdout.write(f"\r    area {n} of {len(boxes)}, {len(seen):,} claims so far…")
        sys.stdout.flush()
        try:
            for f in query(CLAIMS_URL, box, 2000):
                f = slim(f, set(CLAIMS_KEEP))
                key = f["properties"].get("OBJECTID") or json.dumps(f["geometry"])
                seen[key] = f
        except Exception:
            failed += 1
    sys.stdout.write("\r" + " " * 60 + "\r")

    # the box is square, the radius is round — trim the corners
    near = []
    for f in seen.values():
        c = centroid(f["geometry"])
        if not c:
            continue
        if any(km_apart(c[0], c[1], a[0], a[1]) <= CLAIM_RADIUS_KM for a in anchors):
            near.append(f)

    path = os.path.join(DATA, "claims.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": near}, fh)
    print(f"    {len(seen):,} found in those areas -> {len(near):,} within "
          f"{CLAIM_RADIUS_KM:g} km, {os.path.getsize(path)/1048576:.1f} MB")
    if failed:
        print(f"    {failed} area(s) failed to download. Run again to fill them in.")

    total = sum(os.path.getsize(os.path.join(DATA, f)) for f in os.listdir(DATA))
    print(f"\nData total: {total/1048576:.1f} MB")
    print("Done. Open index.html and everything should be there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
