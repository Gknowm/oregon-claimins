#!/usr/bin/env python3
"""
Fills in everything the Oregon map needs. Run it once:

    python3 fetch.py

Covers east of the Cascade crest. Two layers:

  1. Gem and rockhounding occurrences from DOGAMI MILO release 4.
     Filtered to gem categories - no metals, no gravel, no coal.
     That filter is real: MILO records a commodity.

  2. BLM mining claims. Every claim in the box, unfiltered.
     There is no commodity filter here and there cannot be one. BLM
     records no commodity for a claim, so any "gem claims only" rule
     would be a geographic guess wearing a filter's clothes. What is
     in the box is in the file; if a claim is missing, it is outside
     the box, and that is the only reason.

To read commodity onto a claim, look at the gem occurrences sitting on
or beside it. The map tallies those in the claim popup.

To check the sources are alive without downloading anything:

    python3 fetch.py --check

To add gold and other metal occurrences later, set GOLD = True below.
That affects MILO only - claims are already unfiltered. The map has the
Metals layer built in and empty until then.

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

# Include gold and other metals? Off for now. Flip to True and re-run to
# add them; nothing else needs changing.
GOLD = False

# The ground to pull, as west, south, east, north.
# The whole state, coast to Idaho. This used to stop at the Cascade crest
# to keep the Josephine and Jackson gold districts out, because they are
# the claim-densest ground in Oregon and the claims file was polygons.
# Claims are cells now, so those districts cost a few hundred cells
# instead of a few megabytes, and the west side carries the Holley Blue
# and Umpqua carnelian country that the old box cut off.
BBOX = (-124.80, 41.90, -116.40, 46.30)

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

# Only the units that actually produce. These were not guessed - they come
# from asking which map units are over-represented among MILO's agate,
# thunderegg and opal occurrences, measured against ALL MILO occurrences
# in the same unit rather than against unit area. Normalising that way
# controls for where people have looked, which is the confound that sank
# earlier attempts at geology filtering.
#
#   Eastern tholeiitic lavas          20 of 26 occurrences are agate  77%
#   Tuff of Birch Creek                5 of 8                         63%
#   Leslie Gulch Ash-flow Tuff        43 of 69                        62%
#   High-silica rhyolite domes         7 of 12                        58%
#   Volcanic mud flow breccia          7 of 15                        47%
#   Lower tuffaceous sedimentary      19 of 90                        21%
#   John Day Formation                18 of 117                       15%
#
# Thundereggs sit on the silicic half - rhyolite flows and domes at 67%,
# rhyolitic flows at 43% - which is why both sets are here. Agate fills
# cavities in basalt flow tops; thundereggs form in silicic domes. Same
# mineral, different rock.
#
# What this cannot tell you is where nobody has looked. A unit with no
# recorded occurrences reads as barren here whether it is barren or
# simply unwalked.
GEOLOGY_UNITS = [
    "Eastern tholeiitic lavas",
    "Tuff of Birch Creek",
    "Leslie Gulch Ash-flow Tuff",
    "High-silica rhyolite domes and shallow intrusions",
    "Volcanic mud flow breccia",
    "Lower tuffaceous sedimentary rocks",
    "John Day Formation",
    "Rhyolite flows and domes",
    "Rhyolitic flows",
    "Clarno Formation",
    "Porphyritic rhyolite",
]

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

# Also write the full claim polygons to data/claims-detail.geojson.
# Off by default - that file is the 10 MB one.
CLAIM_DETAIL = False

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
    print(f"Box: {BBOX}")
    print(f"Occurrences: gem categories{' plus gold and metals' if GOLD else ' only'}.")
    print("Claims: everything in the box, unfiltered.\n")

    print("Map library")
    vendor()

    # ---- occurrences -------------------------------------------------
    print("\nMineral occurrences")
    print("  Oregon DOGAMI, Mineral Information Layer (MILO)")
    try:
        raw = query(MILO_URL, BBOX, 1000, "MILO")
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

    # Check every name in MILO_KEEP against what the server actually returned.
    #
    # The old version of this guard averaged how many properties survived and
    # complained only if the average fell below four. That cannot catch a
    # single renamed field: v4 corrected CommodityAbreviation to
    # CommodityAbbreviation, 27 of 28 names still matched, the average stayed
    # high, and the one field carrying the mineral name vanished in silence.
    # Every gem collapsed into gem_other and the sunstone records disappeared.
    #
    # So: name the fields that never appeared. Some are legitimately absent
    # because no record in the box fills them, which is why this reports
    # rather than fails - but a renamed field shows up here by name.
    seen = set()
    for f in raw:
        seen.update((f.get("properties") or {}).keys())
    absent = [f for f in MILO_KEEP if f not in seen]
    if absent:
        print(f"    NOTE: {len(absent)} field(s) in MILO_KEEP never appeared:")
        print("      " + ", ".join(absent))
        print("    Either no record in the box fills them, or MILO renamed")
        print("    them. Check the current names at:")
        print("    " + MILO_URL.replace("/query", "?f=pjson"))

    # The mineral name specifically. Commodity only ever says "gemstone
    # material", so if this field is missing the map still draws points but
    # every one of them reads as a generic gem.
    if kept and not any("CommodityAbbreviation" in f["properties"]
                        or "CommodityAbreviation" in f["properties"]
                        for f in kept):
        print("    WARNING: no record carries a commodity abbreviation, so the")
        print("    gem categories below are meaningless. Fix before shipping.")

    path = os.path.join(DATA, "milo.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": kept}, fh)
    print(f"    {len(raw):,} records in box -> {len(kept):,} kept, "
          f"{os.path.getsize(path)/1048576:.1f} MB")

    tally = {}
    for f in kept:
        c = f["properties"]["_cat"]
        tally[c] = tally.get(c, 0) + 1
    for c in sorted(tally, key=lambda k: -tally[k]):
        print(f"      {c:<12} {tally[c]:,}")

    # ---- claims: everything in the box ------------------------------
    # No filter. Not by commodity (BLM records none), not by distance to
    # anything. What is in the box is in the file. If a claim is missing
    # it is because it is outside the box, and nothing else.
    print("\nMining claims")
    print("  BLM Mineral and Land Records System, cases not closed")
    print("  Every claim in the box, whatever it is staked for.")
    try:
        raw = query(CLAIMS_URL, BBOX, 1000, "claims")
    except Exception as exc:
        print(f"    could not download: {exc}")
        return 1
    claims = [slim(f, set(CLAIMS_KEEP)) for f in raw]

    cells, spanning = aggregate_claims(claims, CLAIM_CELL)
    path = os.path.join(DATA, "claims.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": cells}, fh)
    print(f"    {len(claims):,} claims -> {len(cells):,} cells, "
          f"{os.path.getsize(path)/1048576:.1f} MB")
    print(f"      cell edge {CLAIM_CELL} deg, roughly "
          f"{CLAIM_CELL * 69 * 0.73:.1f} x {CLAIM_CELL * 69:.1f} miles")
    if spanning:
        print(f"      {spanning:,} claims span more than one cell and are "
              "counted in each")

    if CLAIM_DETAIL:
        dpath = os.path.join(DATA, "claims-detail.geojson")
        with open(dpath, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": claims}, fh)
        print(f"    full polygons -> claims-detail.geojson, "
              f"{os.path.getsize(dpath)/1048576:.1f} MB")

    # ---- geology: only the units that produce ------------------------
    print("\nGeology")
    print("  Oregon DOGAMI, OGDC-6")
    print(f"  {len(GEOLOGY_UNITS)} map units that MILO shows actually yield")
    print("  agate, thundereggs or opal. Everything else is left out.")
    quoted = ", ".join("'" + u.replace("'", "''") + "'" for u in GEOLOGY_UNITS)
    try:
        raw = query(GEOLOGY_URL, BBOX, 400, "geology",
                    where=f"MAP_UNIT_N IN ({quoted})")
    except Exception as exc:
        print(f"    could not download: {exc}")
        print("    The map works without it - the geology layer will just")
        print("    read as not downloaded. Check the field name is still")
        print("    MAP_UNIT_N at:")
        print("    " + GEOLOGY_URL.replace("/query", "?f=pjson"))
        raw = []

    if raw:
        geo = [slim(f, set(GEOLOGY_KEEP)) for f in raw]
        path = os.path.join(DATA, "geology.geojson")
        with open(path, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": geo}, fh)
        print(f"    {len(geo):,} polygons, {os.path.getsize(path)/1048576:.1f} MB")
        seen = {}
        for f in geo:
            u = f["properties"].get("MAP_UNIT_N") or "?"
            seen[u] = seen.get(u, 0) + 1
        for u in sorted(seen, key=lambda k: -seen[k]):
            print(f"      {seen[u]:>5,}  {u}")
        missing = [u for u in GEOLOGY_UNITS if u not in seen]
        if missing:
            print("    Units with no polygons in the box (check the spelling")
            print("    against the service if you expected them):")
            for u in missing:
                print(f"      {u}")

    total = sum(os.path.getsize(os.path.join(DATA, f)) for f in os.listdir(DATA))
    print(f"\nData total: {total/1048576:.1f} MB")
    print("Done. Open index.html and everything should be there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
