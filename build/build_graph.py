#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the routing graph for All Roads Lead To (Jersey).

Adapted from Doug Belshaw's All Roads Lead To (UK), https://framagit.org/dajbelshaw/all-roads-uk

Inputs (data/raw, made by build/fetch_data.sh):
  roads.opl             every OSM road, lane, path, cycleway and set of steps
  terrain/*.png         Terrarium elevation tiles (zoom 13), for walking and cycling up and down hills
  parishes.geojson      the twelve parishes (admin_level 8)
  buildings.geojsonseq  building footprints, to spread each parish's census population
  pois.geojsonseq       places and landmarks for search and labels
Output:
  graph.json  compact, base64 typed arrays, read by the page

One network serves three ways of travelling: by car, by bike and on foot. Each edge carries
who may use it in which direction (access bits) and a time for each, so walking and cycling
can go up and down hills at different speeds.
"""
import base64, glob, json, math, os, re, struct, sys, zlib
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


MODES = ("car", "bike", "foot")
CAR, BIKE, FOOT = 0, 1, 2


def bit(mode, backward=False):
    return 1 << (2 * mode + (1 if backward else 0))


# class ids: 0 A road, 1 B road, 2 other road, 3 green lane (15 mph), 4 path, cycleway or steps
CLASS = {
    "trunk": 0, "trunk_link": 0, "primary": 0, "primary_link": 0,
    "secondary": 1, "secondary_link": 1,
    "tertiary": 2, "tertiary_link": 2, "unclassified": 2, "residential": 2, "service": 2,
    "living_street": 3,
    "track": 4, "footway": 4, "path": 4, "cycleway": 4, "bridleway": 4, "steps": 4, "pedestrian": 4,
}
# mph, assumed average by car with no traffic, before any posted limit
CAP = {
    "trunk": 34, "primary": 30, "secondary": 27, "tertiary": 25, "unclassified": 23,
    "residential": 18, "living_street": 12,
    "trunk_link": 18, "primary_link": 18, "secondary_link": 18, "tertiary_link": 18,
}
LIMIT_SHARE = 0.85  # average speed as a share of the posted limit
JUNCTION_MIN = 0.05  # 3 seconds for each junction passed by car
BIKE_JUNCTION_MIN = 0.033  # 2 seconds by bike
SLOW_LIMIT = 20  # the "20 mph island" map: every limit cut to this, mph

# Cycling, km/h on the flat: roads and smooth paths, and rough tracks and paths
BIKE_FLAT = 16
BIKE_ROUGH = 11
BIKE_MAX = 30  # downhill
PAVED = {"asphalt", "paved", "concrete", "concrete:plates", "paving_stones", "sett", "chipseal", "metal", "wood"}
STEPS_KMH = 2.5  # on foot, along the steps
NO = ("no", "private")
YES = ("yes", "designated", "permissive", "official", "destination", "customers")
SKIP_SERVICE = ("driveway", "parking_aisle", "drive-through", "parking", "slipway")


def walk_kmh(g):
    """Tobler's hiking function: 5 km/h on the flat, fastest just downhill."""
    return 6 * math.exp(-3.5 * abs(g + 0.05))


def bike_kmh(flat, g):
    if g >= 0:
        return flat / (1 + 10 * g)  # 5% up: two thirds of the flat speed
    return min(flat * (1 - 6 * g), BIKE_MAX)


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

# Where the graph is anchored: for each way of travelling, the strongly connected part of the
# network that contains the Royal Square.
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


# ---------------- elevation ----------------

def read_png(path):
    """Decode an 8-bit RGB or RGBA PNG with the standard library: (width, height, bytes per pixel, pixels)."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    pos, idat = 8, []
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError(f"{path}: only 8-bit RGB/RGBA PNGs without interlacing")
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    i = 0
    for y in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, bpp, out


class Terrain:
    """Heights in metres from Terrarium tiles (height = R*256 + G + B/256 - 32768), sampled bilinearly."""

    def __init__(self, folder):
        self.tiles, self.z = {}, None
        for path in glob.glob(os.path.join(folder, "*.png")):
            m = re.match(r"(\d+)-(\d+)-(\d+)\.png$", os.path.basename(path))
            if not m:
                continue
            z, x, y = map(int, m.groups())
            w, h, bpp, px = read_png(path)
            heights = array("f", (px[k] * 256 + px[k + 1] + px[k + 2] / 256 - 32768 for k in range(0, len(px), bpp)))
            self.tiles[(x, y)] = heights
            self.z = z
        self.n = 256 * 2 ** self.z if self.z is not None else 0

    def pixel(self, gx, gy):
        t = self.tiles.get((gx >> 8, gy >> 8))
        return t[(gy & 255) * 256 + (gx & 255)] if t else 0.0

    def at(self, lon, lat):
        if not self.tiles:
            return 0.0
        fx = (lon + 180) / 360 * self.n - 0.5
        fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * self.n - 0.5
        x0, y0 = math.floor(fx), math.floor(fy)
        tx, ty = fx - x0, fy - y0
        p = self.pixel
        return ((p(x0, y0) * (1 - tx) + p(x0 + 1, y0) * tx) * (1 - ty) +
                (p(x0, y0 + 1) * (1 - tx) + p(x0 + 1, y0 + 1) * tx) * ty)


# ---------------- who may use a way ----------------

def oneway_of(tags):
    v = tags.get("oneway", "")
    if v in ("yes", "true", "1"):
        return True, False
    if v == "-1":
        return False, True
    if tags.get("junction") in ("roundabout", "circular") and v != "no":
        return True, False
    return True, True


def may(tags, key, default):
    """Access for one kind of traveller: its own tag wins, then access=*, then the default."""
    v = tags.get(key)
    if v in NO:
        return False
    if v in YES or v == "dismount":
        return True
    if tags.get("access") in NO:
        return False
    return default


def access_of(tags):
    """Access bits for the three ways of travelling, and whether a bike is pushed here."""
    hw = tags.get("highway")
    if tags.get("area") == "yes" or (hw == "service" and tags.get("service") in SKIP_SERVICE):
        return 0, False
    fwd, bwd = oneway_of(tags)
    acc = 0
    if hw in CAP and may(tags, "motor_vehicle", True) and tags.get("motorcar") not in NO:
        acc |= (bit(CAR) if fwd else 0) | (bit(CAR, True) if bwd else 0)
    push = hw in ("footway", "pedestrian", "steps") or tags.get("bicycle") == "dismount"
    if may(tags, "bicycle", hw != "steps"):
        if tags.get("bicycle") in ("yes", "designated"):
            push = False
        bf, bb = fwd, bwd
        contra = tags.get("oneway:bicycle") == "no" or any(
            str(tags.get(k, "")).startswith("opposite") for k in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both"))
        if push or contra or hw in ("footway", "path", "pedestrian", "steps", "track", "bridleway"):
            bf = bb = True
        acc |= (bit(BIKE) if bf else 0) | (bit(BIKE, True) if bb else 0)
    if may(tags, "foot", True):
        acc |= bit(FOOT) | bit(FOOT, True)
    return acc, push


def flip_acc(acc):
    out = 0
    for m in range(3):
        if acc & bit(m):
            out |= bit(m, True)
        if acc & bit(m, True):
            out |= bit(m)
    return out


def main():
    # ---------------- roads and paths ----------------
    print("reading roads and paths", file=sys.stderr)
    onodes, ways = read_opl(os.path.join(RAW, "roads.opl"))
    print(f"  {len(onodes)} nodes, {len(ways)} ways", file=sys.stderr)
    terrain = Terrain(os.path.join(RAW, "terrain"))
    if terrain.tiles:
        print(f"  {len(terrain.tiles)} elevation tiles at zoom {terrain.z}", file=sys.stderr)
    else:
        print("  no elevation tiles in data/raw/terrain: walking and cycling treat the island as flat", file=sys.stderr)
    height = {}

    def h_of(osm):
        v = height.get(osm)
        if v is None:
            v = height[osm] = terrain.at(*onodes[osm])
        return v

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
        if c is None or len(refs) < 2:
            continue
        acc, push = access_of(tags)
        if not acc:
            continue
        refs = [r for r in refs if r in onodes and on_island(*onodes[r])]
        if len(refs) < 2:
            continue
        lim = parse_mph(tags.get("maxspeed"))
        if c == 2 and (tags.get("green_lane") in ("yes", "true") or (lim and lim <= 15)):
            c = 3
        good.append((c, hw, lim, tags, refs, acc, push))
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

    def climb_times(seg, bike_flat, push, steps):
        """Minutes along seg (OSM node ids) and back, on foot and by bike, sampling heights every ~25 m."""
        wf = wb = bf = bb = 0.0
        for a, b in zip(seg, seg[1:]):
            (lo1, la1), (lo2, la2) = onodes[a], onodes[b]
            x1, y1 = proj(lo1, la1)
            x2, y2 = proj(lo2, la2)
            d = math.hypot(x2 - x1, y2 - y1)
            if d <= 0:
                continue
            k = max(1, math.ceil(d / 0.025))
            hs = [h_of(a)] + [terrain.at(lo1 + (lo2 - lo1) * j / k, la1 + (la2 - la1) * j / k) for j in range(1, k)] + [h_of(b)]
            piece = d / k
            for j in range(k):
                gr = max(-0.25, min(0.25, (hs[j + 1] - hs[j]) / (piece * 1000)))
                if steps:
                    wf += piece / STEPS_KMH * 60
                    wb += piece / STEPS_KMH * 60
                else:
                    wf += piece / walk_kmh(gr) * 60
                    wb += piece / walk_kmh(-gr) * 60
                if push:
                    bf += piece / (walk_kmh(gr) * 0.9) * 60
                    bb += piece / (walk_kmh(-gr) * 0.9) * 60
                else:
                    bf += piece / bike_kmh(bike_flat, gr) * 60
                    bb += piece / bike_kmh(bike_flat, -gr) * 60
        return bf, bb, wf, wb

    # [u, v, cls, car min, km, access, pts, name, 20 mph min, bike min fwd, bwd, foot min fwd, bwd]
    edges = []
    for c, hw, lim, tags, refs, acc, push in good:
        if hw in CAP:
            mph = CAP[hw]
            if lim:
                mph = min(mph, lim * LIMIT_SHARE)
        else:
            mph = 15  # not driven; any number will do
        slow = min(mph, SLOW_LIMIT * LIMIT_SHARE)
        rough = hw in ("track", "path", "bridleway") and tags.get("surface") not in PAVED
        bike_flat = BIKE_ROUGH if rough else BIKE_FLAT
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
                    bf, bb, wf, wb = climb_times(seg, bike_flat, push, hw == "steps")
                    edges.append([g(seg[0]), g(seg[-1]), c, km / (mph * 1.609344) * 60, km, acc, pts, ni,
                                  km / (slow * 1.609344) * 60, bf, bb, wf, wb])
                seg = [r]
    print(f"  {len(gxy)} junctions, {len(edges)} edges before contraction", file=sys.stderr)

    def flipped(e):
        return [e[1], e[0], e[2], e[3], e[4], flip_acc(e[5]), e[6][::-1], e[7], e[8], e[10], e[9], e[12], e[11]]

    # contract chains: nodes touched by exactly two edges of the same class, access and name
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
        if e1[2] != e2[2] or e1[7] != e2[7]:
            continue
        a = e1[:] if e1[1] == n else flipped(e1)
        b = e2[:] if e2[0] == n else flipped(e2)
        if a[0] == n or b[1] == n or a[0] == b[1] or a[5] != b[5]:
            continue
        merged = [a[0], b[1], a[2], a[3] + b[3], a[4] + b[4], a[5], a[6] + b[6][1:], a[7], a[8] + b[8],
                  a[9] + b[9], a[10] + b[10], a[11] + b[11], a[12] + b[12]]
        alive[es[0]] = alive[es[1]] = False
        edges.append(merged)
        alive.append(True)
        ni = len(edges) - 1
        for ep in (merged[0], merged[1]):
            inc[ep].append(ni)
    edges = [e for e, ok in zip(edges, alive) if ok]

    # for each way of travelling, the strongly connected part that contains the Royal Square
    ax, ay = proj(*ANCHOR)
    keep = []
    for m in range(3):
        out_adj, in_adj = defaultdict(list), defaultdict(list)
        for e in edges:
            if e[5] & bit(m):
                out_adj[e[0]].append(e[1]); in_adj[e[1]].append(e[0])
            if e[5] & bit(m, True):
                out_adj[e[1]].append(e[0]); in_adj[e[0]].append(e[1])
        ends = set(out_adj) | set(in_adj)
        start = min(ends, key=lambda i: (gxy[i][0] - ax) ** 2 + (gxy[i][1] - ay) ** 2)

        def reach(adj):
            seen, st = {start}, [start]
            while st:
                u = st.pop()
                for v in adj[u]:
                    if v not in seen:
                        seen.add(v); st.append(v)
            return seen
        keep.append(reach(out_adj) & reach(in_adj))
        print(f"  {MODES[m]}: main component {len(keep[m])} of {len(ends)} nodes", file=sys.stderr)
    for e in edges:
        for m in range(3):
            if e[0] not in keep[m] or e[1] not in keep[m]:
                e[5] &= ~(bit(m) | bit(m, True))
    edges = [e for e in edges if e[5]]
    used = sorted({e[0] for e in edges} | {e[1] for e in edges})
    remap = {o: i for i, o in enumerate(used)}
    nxy = [gxy[o] for o in used]
    mask = [sum(1 << m for m in range(3) if o in keep[m]) for o in used]
    for e in edges:
        e[0], e[1] = remap[e[0]], remap[e[1]]

    # junction costs: half at each end that is a real junction (three or more neighbours) for that traveller
    for m, cost, cols in ((CAR, JUNCTION_MIN, (3, 8)), (BIKE, BIKE_JUNCTION_MIN, (9, 10))):
        nbrs = defaultdict(set)
        for e in edges:
            if e[5] & (bit(m) | bit(m, True)):
                nbrs[e[0]].add(e[1]); nbrs[e[1]].add(e[0])
        for e in edges:
            jc = cost / 2 * ((len(nbrs[e[0]]) >= 3) + (len(nbrs[e[1]]) >= 3))
            for col in cols:
                e[col] += jc
    counts = [sum(1 for k in mask if k & (1 << m)) for m in range(3)]
    print(f"  final {len(nxy)} nodes ({', '.join(f'{MODES[m]} {counts[m]}' for m in range(3))}), {len(edges)} edges", file=sys.stderr)

    # spatial bins for nearest-node queries, one set per way of travelling
    B = 0.5
    bins = [defaultdict(list) for _ in range(3)]
    for i, (x, y) in enumerate(nxy):
        for m in range(3):
            if mask[i] & (1 << m):
                bins[m][(int(math.floor(x / B)), int(math.floor(y / B)))].append(i)

    def near(x, y, maxr, m):
        bx, by = int(math.floor(x / B)), int(math.floor(y / B))
        best, bi = 1e18, -1
        for r in range(maxr + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for i in bins[m].get((bx + dx, by + dy), ()):
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
        rows = defaultdict(list)
        for pts in rings:
            for i in range(len(pts) - 1):
                (x1, y1), (x2, y2) = pts[i], pts[i + 1]
                if y1 == y2:
                    continue
                lo, hi = min(y1, y2), max(y1, y2)
                for iy in range(math.ceil(lo / CELL - 0.5), math.floor(hi / CELL - 0.5) + 1):
                    yc = (iy + 0.5) * CELL
                    if lo <= yc < hi:
                        rows[iy].append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        for iy, xs in rows.items():
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                for ix in range(math.ceil(xs[k] / CELL - 0.5), math.floor(xs[k + 1] / CELL - 0.5) + 1):
                    grid[(ix, iy)] = pi
    print(f"  land {len(grid) * CELL * CELL:.1f} km² on the grid", file=sys.stderr)

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

    # land area: every grid cell goes to its nearest junction, for each way of travelling
    print("land area", file=sys.stderr)
    area = [[0.0] * len(nxy) for _ in range(3)]
    votes = [defaultdict(int) for _ in nxy]
    for (ix, iy), pi in grid.items():
        for m in range(3):
            i = near((ix + 0.5) * CELL, (iy + 0.5) * CELL, 12, m)
            if i >= 0:
                area[m][i] += CELL * CELL
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
    pop = [[0.0] * len(nxy) for _ in range(3)]
    for pi, x, y, w in blds:
        for m in range(3):
            i = near(x, y, 12, m)
            if i >= 0:
                pop[m][i] += CENSUS[PARISHES[pi]] * w / tot_w[pi]
    print(f"  {len(blds)} homes, {sum(pop[CAR]):,.0f} people placed", file=sys.stderr)

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

    # one representative junction per road or path name, for search
    rnodes = defaultdict(set)
    for e in edges:
        if e[7]:
            rnodes[e[7]].update((e[0], e[1]))
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
    print(f"  {len(roads)} named roads and paths", file=sys.stderr)

    churches = []
    for parish_name, name, lon, lat in CHURCHES:
        x, y = proj(lon, lat)
        churches.append({"name": name, "parish": PARISHES.index(parish_name), "x": round(x, 3), "y": round(y, 3)})

    # ---------------- encode ----------------
    def b64(arr):
        return base64.b64encode(arr.tobytes()).decode()

    X = array("f", [p[0] for p in nxy]); Y = array("f", [p[1] for p in nxy])
    EU = array("i"); EV = array("i"); EC = array("b"); EA = array("b")
    ET = array("f"); ES = array("f"); EK = array("f"); EN = array("h"); GO = array("i"); GP = array("h")
    EBF = array("f"); EBB = array("f"); EWF = array("f"); EWB = array("f")
    Q = 1000  # geometry in metres
    for e in edges:
        EU.append(e[0]); EV.append(e[1]); EC.append(e[2]); EA.append(e[5])
        ET.append(e[3]); ES.append(e[8]); EK.append(e[4]); EN.append(e[7])
        EBF.append(e[9]); EBB.append(e[10]); EWF.append(e[11]); EWB.append(e[12])
        pts = dp(e[6], 0.003)
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
        "x": b64(X), "y": b64(Y), "mask": b64(array("b", mask)),
        "eu": b64(EU), "ev": b64(EV), "ec": b64(EC), "ea": b64(EA), "ek": b64(EK), "en": b64(EN),
        "et": b64(ET), "es": b64(ES), "ebf": b64(EBF), "ebb": b64(EBB), "ewf": b64(EWF), "ewb": b64(EWB),
        "go": b64(GO), "gp": b64(GP),
        "area": {MODES[m]: b64(array("f", area[m])) for m in range(3)},
        "pop": {MODES[m]: b64(array("f", pop[m])) for m in range(3)},
        "parish": b64(array("b", parish)),
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
