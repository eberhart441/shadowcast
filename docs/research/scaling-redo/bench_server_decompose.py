"""Where does the end-to-end DashboardServer spend time beyond DashboardState.push + parse?

Feeds the same pre-rendered C5 lines (unpaced) through progressively more of the real server path:
  A  MultiSource only (reader thread per receiver: CSIParser.feed + queue.put), main thread drains
  B  A + server._Reorder (heap, per-receiver marks) in the main thread
  C  full DashboardServer (A + B + lock + DashboardState.push + frames every 0.2 s)
Also a paced variant of C at real time for N=6 (measures CPU fraction while keeping up).
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np

import bench_host_pipeline as b
from csi.multisource import MultiSource
from csi.parser import C5_HEADER
from csi.server import DashboardServer, _Reorder


def build_lines(n, seconds):
    rng = np.random.default_rng(6)
    per_rx = {f"rx{r}": [] for r in range(n)}
    pool = {}
    for rx, p in b.packet_schedule(n, n, seconds, seed=7):
        key = (rx, p.mac)
        if key not in pool:
            pool[key] = [b.make_line(rng, 0, p.mac, 0) for _ in range(8)]
        src = pool[key][len(per_rx[rx]) % 8]
        pre, data = src.split('"[', 1)
        f = pre.split(",")
        f[9] = str(p.device_ts % 2**32)
        per_rx[rx].append((p.host_ts, ",".join(f) + '"[' + data))
    return per_rx


def run_A(per_rx, reorder=False):
    ms = MultiSource({rx: iter([(0.0, C5_HEADER)] + v) for rx, v in per_rx.items()})
    total = sum(len(v) for v in per_rx.values())
    ro = _Reorder() if reorder else None
    got = 0
    c0, t0 = time.process_time(), time.perf_counter()
    ms.start()
    while not ms.finished():
        batch = ms.drain(timeout=0.05)
        if ro is not None:
            for rx, p in batch:
                ro.add(rx, p)
            got += len(ro.pop_ready(set(per_rx)))
        else:
            got += len(batch)
    if ro is not None:
        got += len(ro.pop_ready(set(per_rx), flush=True))
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    ms.close()
    return {"packets": total, "got": got, "wall_us_per_pkt": wall / total * 1e6, "cpu_us_per_pkt": cpu / total * 1e6}


def run_C(n, per_rx, paced=False):
    room = b.make_room(n, n)
    thr = {f"rx{r}<-{b.mac_of(s)[-2:]}": 0.5 for r in range(n) for s in range(n)}
    total = sum(len(v) for v in per_rx.values())
    if paced:
        # re-stamp with the wall clock and release each line at its time (like a serial port)
        start = time.time() + 0.5
        t_first = min(v[0][0] for v in per_rx.values())
        def paced_iter(v):
            yield 0.0, C5_HEADER
            for ts, line in v:
                due = start + (ts - t_first)
                d = due - time.time()
                if d > 0:
                    time.sleep(d)
                yield time.time(), line
        sources = {rx: paced_iter(v) for rx, v in per_rx.items()}
    else:
        sources = {rx: iter([(0.0, C5_HEADER)] + v) for rx, v in per_rx.items()}
    srv = DashboardServer(room, sources, thr, host="127.0.0.1", port=0)
    c0, t0 = time.process_time(), time.perf_counter()
    srv.start()
    srv.wait_finished(timeout=900)
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    srv.stop()
    return {"packets": total, "wall_s": wall, "cpu_s": cpu, "wall_us_per_pkt": wall / total * 1e6,
            "cpu_us_per_pkt": cpu / total * 1e6, "cpu_fraction_of_one_core": cpu / wall}


if __name__ == "__main__":
    out = {}
    for n in (2, 4, 6):
        per_rx = build_lines(n, 15.0)
        r = {"N": n, "A_multisource_parse": run_A(per_rx), "B_plus_reorder": run_A(per_rx, reorder=True),
             "C_full_server_unpaced": run_C(n, per_rx)}
        print(json.dumps(r), flush=True)
        out[f"N{n}"] = r
    for n in (4, 5, 6):
        per_rx = build_lines(n, 20.0)
        r = run_C(n, per_rx, paced=True)
        r["N"] = n
        print(json.dumps({"paced": r}), flush=True)
        out[f"paced_N{n}"] = r
    with open("bench_results_server_decompose.json", "w") as f:
        json.dump(out, f, indent=1)
    print("wrote")
