#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Score every junction as a meeting place for everyone in Jersey: central.json, for the "Everyone in Jersey" meet-up.

For each way of travelling (car, bike, on foot) and each measure (fastest time, shortest distance), and for every
junction c, find the journey to c from every other junction (one search per junction, run backwards from c), then keep:
  maxLand, maxPop    the longest journey, from any junction with land, or with people
  meanLand, meanPop  the average journey, weighted by land area, or by people
The page picks the junction with the lowest score ("nobody travels too long": the longest; "least total travel": the
average) and shades every junction by its score.

Reads graph.json (python3 build/build_graph.py first); runs the searches on every core: a few minutes in all.
Times are kept in tenths of a minute and distances in tens of metres, as Uint16 (65535: not reachable that way).
"""
import base64, heapq, json, math, os, sys, time
from array import array
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODES = ("car", "bike", "foot")
NONE = 65535

G = {}  # set in each worker by init()


def dec(b64, typ):
    a = array(typ)
    a.frombytes(base64.b64decode(b64))
    return a


def init(path):
    d = json.load(open(path, encoding="utf-8"))
    G.update(n=d["n"], m=d["m"], eu=dec(d["eu"], "i"), ev=dec(d["ev"], "i"), ea=dec(d["ea"], "b"), mask=dec(d["mask"], "b"),
             cost={"car": (dec(d["et"], "f"),) * 2, "bike": (dec(d["ebf"], "f"), dec(d["ebb"], "f")),
                   "foot": (dec(d["ewf"], "f"), dec(d["ewb"], "f"))},
             km=dec(d["ek"], "f"),
             area={t: dec(d["area"][t], "f") for t in MODES}, pop={t: dec(d["pop"][t], "f") for t in MODES})


def reverse_graph(mode, metric):
    """For each junction, the junctions that can reach it in one step, and the cost of that step."""
    n, mi = G["n"], MODES.index(mode)
    fwd, bwd = (G["km"], G["km"]) if metric == "dist" else G["cost"][mode]
    rin = [[] for _ in range(n)]
    eu, ev, ea = G["eu"], G["ev"], G["ea"]
    for e in range(G["m"]):
        if ea[e] & (1 << (2 * mi)):
            rin[ev[e]].append((eu[e], fwd[e]))
        if ea[e] & (1 << (2 * mi + 1)):
            rin[eu[e]].append((ev[e], bwd[e]))
    return rin


def score_chunk(args):
    mode, metric, targets = args
    key = (mode, metric)
    if G.get("rin_key") != key:
        G["rin"], G["rin_key"] = reverse_graph(mode, metric), key
    rin, n = G["rin"], G["n"]
    land, pop = G["area"][mode], G["pop"][mode]
    has_land = [i for i in range(n) if land[i] > 0]
    has_pop = [i for i in range(n) if pop[i] > 0]
    L, P = sum(land), sum(pop)
    inf = math.inf
    out = []
    for c in targets:
        dist = [inf] * n
        dist[c] = 0.0
        h = [(0.0, c)]
        pop_ = heapq.heappop
        push = heapq.heappush
        while h:
            du, u = pop_(h)
            if du > dist[u]:
                continue
            for w, cw in rin[u]:
                nd = du + cw
                if nd < dist[w]:
                    dist[w] = nd
                    push(h, (nd, w))
        out.append((c, max(dist[i] for i in has_land), max(dist[i] for i in has_pop),
                    sum(dist[i] * land[i] for i in has_land) / L, sum(dist[i] * pop[i] for i in has_pop) / P))
    return key, out


def main():
    graph = os.path.join(ROOT, "graph.json")
    init(graph)
    n, mask = G["n"], G["mask"]
    jobs = []
    for mode in MODES:
        nodes = [i for i in range(n) if mask[i] & (1 << MODES.index(mode))]
        for metric in ("time", "dist"):
            step = max(50, len(nodes) // 64)
            jobs += [(mode, metric, nodes[k:k + step]) for k in range(0, len(nodes), step)]
    t0 = time.time()
    res = {(t, m): [] for t in MODES for m in ("time", "dist")}
    with Pool(initializer=init, initargs=(graph,)) as pool:
        done = 0
        for key, rows in pool.imap_unordered(score_chunk, jobs):
            res[key] += rows
            done += 1
            if done % 32 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} batches, {time.time() - t0:.0f} s", file=sys.stderr)

    def b64(arr):
        return base64.b64encode(arr.tobytes()).decode()

    out = {"n": n, "time_unit": 0.1, "dist_unit": 0.01, "scores": {}}
    for (mode, metric), rows in res.items():
        unit = 0.1 if metric == "time" else 0.01  # minutes, km
        cols = {k: array("H", [NONE] * n) for k in ("maxLand", "maxPop", "meanLand", "meanPop")}
        best = {}
        for c, *vals in rows:
            for k, v in zip(cols, vals):
                cols[k][c] = min(NONE - 1, round(v / unit)) if math.isfinite(v) else NONE
                if math.isfinite(v) and (k not in best or v < best[k][1]):
                    best[k] = (c, v)
        out["scores"][f"{mode}|{metric}"] = {k: b64(a) for k, a in cols.items()}
        print(f"  {mode} by {metric}: " + ", ".join(f"{k} {best[k][1]:.1f} at junction {best[k][0]}" for k in cols), file=sys.stderr)
    with open(os.path.join(ROOT, "central.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote central.json {os.path.getsize(os.path.join(ROOT, 'central.json')) / 1e6:.2f} MB in {time.time() - t0:.0f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
