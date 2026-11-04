#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the routing graph for All Roads Lead To (Jersey).

Adapted from Doug Belshaw's All Roads Lead To (UK), https://framagit.org/dajbelshaw/all-roads-uk

Inputs (data/raw, made by build/fetch_data.sh):
  roads.opl             every OSM road a car can use, from A roads to green lanes
  parishes.geojson      the twelve parishes (admin_level 8)
  buildings.geojsonseq  building footprints, to spread each parish's census population
  pois.geojsonseq       places and landmarks for search and labels
Output:
  graph.json  compact, base64 typed arrays, read by the page
"""
import base64, json, math, os, re, sys
from array import array
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")

LAT0, LON0 = 49.21, -2.13
KX = math.cos(math.radians(LAT0)) * 111.320  # km per degree lon
KY = 111.2  # km per degree lat at 49°N

# The main island. Les Écréhous and Les Minquiers belong to parishes too, but have no roads.
ISLAND = (-2.27, 49.15, -1.99, 49.28)


def proj(lon, lat):
    return ((lon - LON0) * KX, (lat - LAT0) * KY)  # km, y up


# class ids: 0 A road, 1 B road, 2 other road, 3 green lane (15 mph)
CLASS = {
    "trunk": 0, "trunk_link": 0, "primary": 0, "primary_link": 0,
    "secondary": 1, "secondary_link": 1,
    "tertiary": 2, "tertiary_link": 2, "unclassified": 2, "residential": 2,
    "living_street": 3,
}
# mph, assumed average with no traffic, before any posted limit
CAP = {
    "trunk": 34, "primary": 30, "secondary": 27, "tertiary": 25, "unclassified": 23,
    "residential": 18, "living_street": 12,
    "trunk_link": 18, "primary_link": 18, "secondary_link": 18, "tertiary_link": 18,
}
LIMIT_SHARE = 0.85  # average speed as a share of the posted limit
JUNCTION_MIN = 0.05  # 3 seconds for each junction passed
SLOW_LIMIT = 20  # the "20 mph island" map: every limit cut to this, mph

# Census 2021, Statistics Jersey, Bulletin 1: population characteristics (total 103,267)
CENSUS = {
    "St Helier": 35822, "St Saviour": 13904, "St Brelade": 11012, "St Clement": 9925,
    "St Lawrence": 5561, "Grouville": 5401, "St Peter": 5264, "St Ouen": 4206,
    "St Martin": 3948, "Trinity": 3355, "St John": 3051, "St Mary": 1818,
}
PARISHES = ["St Helier", "St Saviour", "St Brelade", "St Clement", "St Lawrence", "Grouville",
            "St Peter", "St Ouen", "St Martin", "Trinity", "St John", "St Mary"]

# The parish churches, from OpenStreetMap. (parish, name, lon, lat)
CHURCHES = [
    ("St Helier", "St Helier Parish Church", -2.10677, 49.18346),
    ("St Saviour", "St Saviour's Church", -2.09163, 49.19529),
    ("St Brelade", "St Brelade's Church", -2.20291, 49.18401),
    ("St Clement", "St Clement's Church", -2.05662, 49.17374),
    ("St Lawrence", "St Lawrence Parish Church", -2.14152, 49.21453),
    ("Grouville", "Grouville Church", -2.05025, 49.18377),
    ("St Peter", "St Peter's Church", -2.18381, 49.21241),
    ("St Ouen", "St Ouen's Church", -2.20701, 49.22546),
    ("St Martin", "St Martin's Church", -2.05111, 49.21841),
    ("Trinity", "Trinity Church", -2.09165, 49.23384),
    ("St John", "St John's Parish Church", -2.13897, 49.24607),
    ("St Mary", "St Mary's Church", -2.17372, 49.23496),
]

# Where the graph is anchored: the strongly connected part of the network that contains the Royal Square.
ANCHOR = (-2.10558, 49.18386)


def unesc(s):
    return re.sub(r"%([0-9a-f]+)%", lambda m: chr(int(m.group(1), 16)), s)


def parse_tags(s):
    out = {}
    if not s:
        return out
    for kv in s.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[unesc(k)] = unesc(v)
    return out


def read_opl(path):
    nodes, ways = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ")
            kind = parts[0][0]
            fields = {p[0]: p[1:] for p in parts[1:] if p}
            if kind == "n":
                if fields.get("x"):
                    nodes[int(parts[0][1:])] = (float(fields["x"]), float(fields["y"]))
            elif kind == "w":
                refs = [int(r[1:]) for r in fields.get("N", "").split(",") if r]
                ways.append((parse_tags(fields.get("T", "")), refs))
    return nodes, ways


def read_seq(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if line:
                yield json.loads(line)


def parse_mph(v):
    if not v:
        return None
    m = re.match(r"(\d+)\s*(mph)?", v)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2) else n * 0.621  # bare numbers are km/h in OSM


def tidy(name):
    """St. Brelade -> St Brelade, Saint Ouen -> St Ouen."""
    name = re.sub(r"\bSt\.\s*", "St ", name.strip())
    return re.sub(r"^Saint ", "St ", name)


def dp(pts, tol):
    """Douglas-Peucker on a list of (x, y)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def flat(coords):
    if isinstance(coords[0], (int, float)):
        yield coords
    else:
        for c in coords:
            yield from flat(c)


def on_island(lon, lat):
    return ISLAND[0] <= lon <= ISLAND[2] and ISLAND[1] <= lat <= ISLAND[3]


def ring_area(pts):
    a = 0.0
    for i in range(len(pts) - 1):
        a += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return a / 2


def oneway_of(tags):
    v = tags.get("oneway", "")
    if v in ("yes", "true", "1"):
        return True, False
    if v == "-1":
        return False, True
    if tags.get("junction") in ("roundabout", "circular") and v != "no":
        return True, False
    return True, True


def drivable(tags):
    if tags.get("access") in ("no", "private"):
        return False
    if tags.get("motor_vehicle") in ("no", "private") or tags.get("motorcar") in ("no", "private"):
        return False
    if tags.get("area") == "yes":
        return False
    return True


def main():
    # ---------------- roads ----------------
    print("reading roads", file=sys.stderr)
    onodes, ways = read_opl(os.path.join(RAW, "roads.opl"))
    print(f"  {len(onodes)} nodes, {len(ways)} ways", file=sys.stderr)

    names, name_ix = [""], {"": 0}

    def nid(s):
        i = name_ix.get(s)
        if i is None:
            i = name_ix[s] = len(names)
            names.append(s)
        return i

    use = defaultdict(int)
    good = []
    for tags, refs in ways:
        hw = tags.get("highway")
        c = CLASS.get(hw)
        if c is None or len(refs) < 2 or not drivable(tags):
            continue
        refs = [r for r in refs if r in onodes and on_island(*onodes[r])]
        if len(refs) < 2:
            continue
        lim = parse_mph(tags.get("maxspeed"))
        if c == 2 and (tags.get("green_lane") in ("yes", "true") or (lim and lim <= 15)):
            c = 3
        good.append((c, hw, lim, tags, refs))
        use[refs[0]] += 2
        use[refs[-1]] += 2
        for r in refs[1:-1]:
            use[r] += 1

    gid, gxy = {}, []

    def g(osm):
        i = gid.get(osm)
        if i is None:
            i = gid[osm] = len(gxy)
            gxy.append(proj(*onodes[osm]))
        return i

    edges = []  # [u, v, cls, minutes, km, fwd, bwd, pts, name, minutes at 20 mph]
    for c, hw, lim, tags, refs in good:
        fwd, bwd = oneway_of(tags)
        mph = CAP[hw]
        if lim:
            mph = min(mph, lim * LIMIT_SHARE)
        slow = min(mph, SLOW_LIMIT * LIMIT_SHARE)
        nm = tags.get("name", "").strip()
        ref = tags.get("ref", "").strip()
        label = f"{nm} ({ref})" if nm and ref else (nm or ref)
        ni = nid(label)
        seg = [refs[0]]
        for r in refs[1:]:
            seg.append(r)
            if use[r] > 1 or r == refs[-1]:
                pts = [proj(*onodes[s]) for s in seg]
                km = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))
                if km > 0 and seg[0] != seg[-1]:
                    edges.append([g(seg[0]), g(seg[-1]), c, km / (mph * 1.609344) * 60, km, fwd, bwd, pts, ni,
                                  km / (slow * 1.609344) * 60])
                seg = [r]
    print(f"  {len(gxy)} junctions, {len(edges)} edges before contraction", file=sys.stderr)

    # contract chains: nodes touched by exactly two edges of the same class, direction and name
    alive = [True] * len(edges)
    inc = defaultdict(list)
    for ei, e in enumerate(edges):
        inc[e[0]].append(ei)
        inc[e[1]].append(ei)
    for n in range(len(gxy)):
        es = [ei for ei in inc[n] if alive[ei]]
        if len(es) != 2 or es[0] == es[1]:
            continue
        e1, e2 = edges[es[0]], edges[es[1]]
        if e1[2] != e2[2] or e1[8] != e2[8]:
            continue

        def orient(e, end):
            if (e[1] == n) == end:
                return e[:]
            return [e[1], e[0], e[2], e[3], e[4], e[6], e[5], e[7][::-1], e[8], e[9]]
        a = orient(e1, True)
        b = orient(e2, False)
        if a[0] == n or b[1] == n or a[0] == b[1]:
            continue
        if (a[5], a[6]) != (b[5], b[6]):
            continue
        merged = [a[0], b[1], a[2], a[3] + b[3], a[4] + b[4], a[5], a[6], a[7] + b[7][1:], a[8], a[9] + b[9]]
        alive[es[0]] = alive[es[1]] = False
        edges.append(merged)
        alive.append(True)
        ni = len(edges) - 1
        for ep in (merged[0], merged[1]):
            inc[ep].append(ni)
    edges = [e for e, ok in zip(edges, alive) if ok]

    # strongly connected component that contains the Royal Square
    out_adj, in_adj = defaultdict(list), defaultdict(list)
    for e in edges:
        if e[5]:
            out_adj[e[0]].append(e[1]); in_adj[e[1]].append(e[0])
        if e[6]:
            out_adj[e[1]].append(e[0]); in_adj[e[0]].append(e[1])
    ends = {x for e in edges for x in e[:2]}
    ax, ay = proj(*ANCHOR)
    start = min(ends, key=lambda i: (gxy[i][0] - ax) ** 2 + (gxy[i][1] - ay) ** 2)

    def reach(adj):
        seen, st = {start}, [start]
        while st:
            u = st.pop()
            for v in adj[u]:
                if v not in seen:
                    seen.add(v); st.append(v)
        return seen
    keep = reach(out_adj) & reach(in_adj)
    print(f"  main component {len(keep)} of {len(ends)} nodes", file=sys.stderr)

    edges = [e for e in edges if e[0] in keep and e[1] in keep]
    used = sorted({e[0] for e in edges} | {e[1] for e in edges})
    remap = {o: i for i, o in enumerate(used)}
    nxy = [gxy[o] for o in used]
    for e in edges:
        e[0], e[1] = remap[e[0]], remap[e[1]]

    # junction cost: half at each end that is a real junction (three or more neighbours)
    nbrs = defaultdict(set)
    for e in edges:
        nbrs[e[0]].add(e[1]); nbrs[e[1]].add(e[0])
    for e in edges:
        jc = JUNCTION_MIN / 2 * ((len(nbrs[e[0]]) >= 3) + (len(nbrs[e[1]]) >= 3))
        e[3] += jc
        e[9] += jc
    print(f"  final {len(nxy)} nodes, {len(edges)} edges", file=sys.stderr)

    # spatial bins for nearest-node queries
    B = 0.5
    bins = defaultdict(list)
    for i, (x, y) in enumerate(nxy):
        bins[(int(math.floor(x / B)), int(math.floor(y / B)))].append(i)

    def near(x, y, maxr=8):
        bx, by = int(math.floor(x / B)), int(math.floor(y / B))
        best, bi = 1e18, -1
        for r in range(maxr + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for i in bins.get((bx + dx, by + dy), ()):
                        d = (nxy[i][0] - x) ** 2 + (nxy[i][1] - y) ** 2
                        if d < best:
                            best, bi = d, i
            if bi >= 0 and math.sqrt(best) < r * B:
                break
        return bi

    # ---------------- parishes ----------------
    print("parishes", file=sys.stderr)
    pj = json.load(open(os.path.join(RAW, "parishes.geojson"), encoding="utf-8"))
    rings_of = defaultdict(list)  # parish idx -> rings (projected), outer and inner alike
    outline = []
    for f in pj["features"]:
        p = f["properties"]
        if p.get("admin_level") != "8" or p.get("boundary") != "administrative":
            continue
        name = tidy(p.get("name", ""))
        if name not in PARISHES:
            if name:
                print(f"  skipping unknown parish {name!r}", file=sys.stderr)
            continue  # unnamed member ways that came along with the relations
        pi = PARISHES.index(name)
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            lon, lat = poly[0][0]
            if not on_island(lon, lat):
                continue  # offshore reefs
            for ring in poly:
                pts = [proj(x, y) for x, y in ring]
                rings_of[pi].append(pts)
                if abs(ring_area(pts)) < 0.0004:
                    continue  # rocks under 400 m² vanish at this scale
                h = len(pts) // 2  # closed ring: simplify each half so the endpoints differ
                simple = dp(pts[:h + 1], 0.006) + dp(pts[h:], 0.006)[1:]
                if len(simple) > 3:
                    outline.append([pi, [[round(x, 3), round(y, 3)] for x, y in simple]])
    missing = [PARISHES[i] for i in range(len(PARISHES)) if i not in rings_of]
    if missing:
        sys.exit(f"parishes missing from parishes.geojson: {missing}")

    # rasterise the parishes on a 50 m grid by scanline (even-odd across each parish's rings)
    CELL = 0.05
    grid = {}  # (ix, iy) -> parish
    for pi, rings in rings_of.items():
        segs = []
        ymin, ymax = 1e9, -1e9
        for pts in rings:
            for i in range(len(pts) - 1):
                (x1, y1), (x2, y2) = pts[i], pts[i + 1]
                if y1 != y2:
                    segs.append((x1, y1, x2, y2))
                ymin, ymax = min(ymin, y1), max(ymax, y1)
        rows = defaultdict(list)
        for x1, y1, x2, y2 in segs:
            lo, hi = min(y1, y2), max(y1, y2)
            iy0 = math.ceil(lo / CELL - 0.5)
            iy1 = math.floor(hi / CELL - 0.5)
            for iy in range(iy0, iy1 + 1):
                yc = (iy + 0.5) * CELL
                if lo <= yc < hi:
                    rows[iy].append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        for iy, xs in rows.items():
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                for ix in range(math.ceil(xs[k] / CELL - 0.5), math.floor(xs[k + 1] / CELL - 0.5) + 1):
                    grid[(ix, iy)] = pi
    land_km2 = len(grid) * CELL * CELL
    print(f"  land {land_km2:.1f} km² on the grid", file=sys.stderr)

    def parish_at(x, y, reach=6):
        ix, iy = math.floor(x / CELL), math.floor(y / CELL)
        if (ix, iy) in grid:
            return grid[(ix, iy)]
        for r in range(1, reach + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) == r and (ix + dx, iy + dy) in grid:
                        return grid[(ix + dx, iy + dy)]
        return -1

    # land area: every grid cell goes to its nearest junction
    print("land area", file=sys.stderr)
    area = [0.0] * len(nxy)
    votes = [defaultdict(int) for _ in nxy]
    for (ix, iy), pi in grid.items():
        i = near((ix + 0.5) * CELL, (iy + 0.5) * CELL, 12)
        if i >= 0:
            area[i] += CELL * CELL
            votes[i][pi] += 1
    parish = []
    for i, (x, y) in enumerate(nxy):
        pi = parish_at(x, y, 0)
        if pi < 0:
            pi = max(votes[i], key=votes[i].get) if votes[i] else parish_at(x, y, 40)
        parish.append(max(pi, 0))

    # ---------------- people ----------------
    print("population", file=sys.stderr)
    HOMES = {"yes", "house", "apartments", "residential", "semidetached_house", "terrace", "detached",
             "bungalow", "farm", "cottage", "dormitory", "flats"}
    NOT_HOMES = ("amenity", "shop", "office", "tourism", "craft", "industrial", "leisure", "man_made",
                 "historic", "military", "religion", "healthcare", "public_transport", "aeroway")
    blds = []  # (parish, x, y, weight)
    for f in read_seq(os.path.join(RAW, "buildings.geojsonseq")):
        p = f["properties"]
        b = p.get("building")
        if b not in HOMES or any(k in p for k in NOT_HOMES):
            continue
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        a, cx, cy = 0.0, 0.0, 0.0
        for poly in polys:
            for ri, ring in enumerate(poly):
                pts = [proj(x, y) for x, y in ring]
                ra = abs(ring_area(pts)) * (1 if ri == 0 else -1)
                a += ra
                if ri == 0:
                    n = len(pts) - 1 or 1
                    cx += ra * sum(q[0] for q in pts[:-1]) / n
                    cy += ra * sum(q[1] for q in pts[:-1]) / n
        m2 = a * 1e6
        if m2 < 30 or (b == "yes" and m2 > 1500) or m2 > 6000:
            continue  # sheds and garages; big unlabelled sheds, glasshouses and warehouses
        x, y = cx / a, cy / a
        try:
            lv = float(re.match(r"[\d.]+", p.get("building:levels", "")).group(0))
        except (AttributeError, ValueError):
            lv = 3 if b in ("apartments", "flats", "dormitory") else 1.6
        pi = parish_at(x, y)
        if pi >= 0:
            blds.append((pi, x, y, m2 * max(1, min(lv, 12))))
    tot_w = defaultdict(float)
    for pi, _, _, w in blds:
        tot_w[pi] += w
    pop = [0.0] * len(nxy)
    for pi, x, y, w in blds:
        i = near(x, y, 12)
        if i >= 0:
            pop[i] += CENSUS[PARISHES[pi]] * w / tot_w[pi]
    print(f"  {len(blds)} homes, {sum(pop):,.0f} people placed", file=sys.stderr)

    # ---------------- places, landmarks, roads ----------------
    print("places", file=sys.stderr)
    PLACE_RANK = {"town": 0, "village": 1, "suburb": 2, "hamlet": 3, "square": 4, "locality": 5, "isolated_dwelling": 6}
    LANDMARK = [  # (key, values, label)
        ("aeroway", ("aerodrome",), "Airport"),
        ("amenity", ("hospital",), "Hospital"),
        ("amenity", ("ferry_terminal",), "Ferry terminal"),
        ("historic", ("castle", "fort"), "Castle"),
        ("man_made", ("lighthouse",), "Lighthouse"),
        ("natural", ("beach",), "Beach"),
        ("amenity", ("school",), "School"),
        ("tourism", ("zoo", "attraction", "museum"), "Attraction"),
    ]
    seen = {}
    for f in read_seq(os.path.join(RAW, "pois.geojsonseq")):
        p = f["properties"]
        name = tidy(p.get("name", ""))
        if not name:
            continue
        pts = list(flat(f["geometry"]["coordinates"]))
        lon = sum(q[0] for q in pts) / len(pts)
        lat = sum(q[1] for q in pts) / len(pts)
        if not on_island(lon, lat):
            continue
        rank, kind = None, None
        if p.get("place") in PLACE_RANK:
            rank, kind = PLACE_RANK[p["place"]], p["place"]
        else:
            for li, (k, vals, label) in enumerate(LANDMARK):
                if p.get(k) in vals:
                    rank, kind = 10 + li, label
                    break
        if rank is None:
            continue
        key = name.lower()
        if key in seen and seen[key][0] <= rank:
            continue
        x, y = proj(lon, lat)
        seen[key] = (rank, name, kind, x, y)
    for parish_name, name, lon, lat in CHURCHES:
        x, y = proj(lon, lat)
        seen[name.lower()] = (9, name, "Parish church", x, y)
    places = []
    for rank, name, kind, x, y in sorted(seen.values()):
        places.append({"name": name, "kind": kind, "x": round(x, 3), "y": round(y, 3), "p": parish_at(x, y, 40)})
    print(f"  {len(places)} places and landmarks", file=sys.stderr)

    # one representative junction per road name, for search
    rnodes = defaultdict(set)
    for e in edges:
        if e[8]:
            rnodes[e[8]].update((e[0], e[1]))
    roads = []
    for ni, ns in rnodes.items():
        label = names[ni]
        base = re.sub(r" \([^)]*\)$", "", label)
        if not base or re.fullmatch(r"[A-Z]\d+", base):
            continue
        mx = sum(nxy[i][0] for i in ns) / len(ns)
        my = sum(nxy[i][1] for i in ns) / len(ns)
        roads.append([base, min(ns, key=lambda i: (nxy[i][0] - mx) ** 2 + (nxy[i][1] - my) ** 2)])
    roads.sort()
    dedup = {}
    for nm, node in roads:
        dedup.setdefault(nm, node)  # a name used twice keeps its first stretch
    roads = [[nm, node] for nm, node in dedup.items()]
    print(f"  {len(roads)} named roads", file=sys.stderr)

    churches = []
    for parish_name, name, lon, lat in CHURCHES:
        x, y = proj(lon, lat)
        churches.append({"name": name, "parish": PARISHES.index(parish_name), "x": round(x, 3), "y": round(y, 3)})

    # ---------------- encode ----------------
    def b64(arr):
        return base64.b64encode(arr.tobytes()).decode()

    X = array("f", [p[0] for p in nxy]); Y = array("f", [p[1] for p in nxy])
    EU = array("i"); EV = array("i"); EC = array("b"); ED = array("b")
    ET = array("f"); ES = array("f"); EK = array("f"); EN = array("h"); GO = array("i"); GP = array("h")
    Q = 1000  # geometry in metres
    for e in edges:
        EU.append(e[0]); EV.append(e[1]); EC.append(e[2]); ED.append((1 if e[5] else 0) | (2 if e[6] else 0))
        ET.append(e[3]); ES.append(e[9]); EK.append(e[4]); EN.append(e[8])
        pts = dp(e[7], 0.003)
        GO.append(len(GP) // 2)
        px, py = round(pts[0][0] * Q), round(pts[0][1] * Q)
        for (x, y) in pts[1:-1]:
            qx, qy = round(x * Q), round(y * Q)
            dx, dy = qx - px, qy - py
            while abs(dx) > 32000 or abs(dy) > 32000:  # split long hops
                sx = max(-32000, min(32000, dx)); sy = max(-32000, min(32000, dy))
                GP.append(sx); GP.append(sy); px += sx; py += sy; dx -= sx; dy -= sy
            GP.append(dx); GP.append(dy)
            px, py = qx, qy
    GO.append(len(GP) // 2)

    out = {
        "proj": {"lat0": LAT0, "lon0": LON0, "kx": KX, "ky": KY, "q": Q},
        "n": len(nxy), "m": len(edges),
        "x": b64(X), "y": b64(Y),
        "eu": b64(EU), "ev": b64(EV), "ec": b64(EC), "ed": b64(ED), "et": b64(ET), "es": b64(ES), "ek": b64(EK), "en": b64(EN),
        "go": b64(GO), "gp": b64(GP),
        "area": b64(array("f", area)), "pop": b64(array("f", pop)), "parish": b64(array("b", parish)),
        "slow_limit": SLOW_LIMIT,
        "parishes": PARISHES,
        "census": [CENSUS[p] for p in PARISHES],
        "outline": outline,
        "names": names,
        "places": places,
        "roads": roads,
        "churches": churches,
    }
    with open(os.path.join(ROOT, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    print(f"wrote graph.json {os.path.getsize(os.path.join(ROOT, 'graph.json'))/1e6:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
