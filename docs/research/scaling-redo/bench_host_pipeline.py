"""Host-pipeline scaling benchmark for Team-12-tracker (feat/wireless-receivers).

Not part of the repo. Run with the research venv and PYTHONPATH pointing at the checkout:

    PYTHONPATH=../../Team-12-tracker _venv/Scripts/python bench_host_pipeline.py [stage ...]

Stages (default: all):
    line      bytes per realistic ESP32-C5 CSI_DATA line (234 ints) and serial ceilings
    parser    CSIParser.feed cost per line
    reader    serial reader: pyserial readline() (per-byte) vs the chunked reader, on a fake in-memory port
    gateway   split_gateway demux cost per line (one gateway thread for every receiver)
    pipeline  DashboardState.push per packet (steady state, fixed and auto) + frame snapshot/json for N = 2..12
    server    end-to-end DashboardServer (MultiSource threads, parse, reorder, lock, frames) throughput, N = 2..10
    profile   cProfile of the steady-state pipeline at N = 8

All N: N receivers x N senders (N^2 links), as the brief asks. An all-transceiver rig of N boards has N(N-1).
"""
from __future__ import annotations

import cProfile
import io
import json
import math
import pstats
import queue
import sys
import threading
import time

import numpy as np

from csi.dashboard import DashboardState
from csi.parser import CSIPacket, CSIParser, C5_HEADER
from csi.room import Node, Room

RATE_HZ = 40.0
N_RAW = 234          # ESP32-C5 raw ints (117 subcarriers), parser KNOWN_NULL_SUBCARRIERS
N_KEPT = 114         # after removing 3 nulls
NS = [2, 3, 4, 5, 6, 8, 10, 12]
OUT = {}


def mac_of(j: int) -> str:
    return f"1a:00:00:00:00:{j:02x}"


def make_room(n_rx: int, n_tx: int) -> Room:
    w, d = 4.0, 3.0
    def perim(k, n, off):
        # spread nodes around the perimeter
        s = (k + off) / n * 2 * (w + d)
        if s < w: return s, 0.0
        s -= w
        if s < d: return w, s
        s -= d
        if s < w: return w - s, d
        return 0.0, d - (s - w)
    rx = tuple(Node(f"rx{i}", "receiver", *perim(i, n_rx, 0.0), chip="ESP32-C5") for i in range(n_rx))
    tx = tuple(Node(f"tx{j}", "sender", *perim(j, n_tx, 0.5), mac=mac_of(j), chip="ESP32-C5") for j in range(n_tx))
    return Room(w, d, rx, tx)


def make_line(rng, seq: int, mac: str, ts_us: int, sigma: float = 40.0) -> str:
    vals = np.clip(np.round(rng.normal(0, sigma, N_RAW)), -128, 127).astype(int)
    data = ",".join(map(str, vals))
    return (f"CSI_DATA,{seq},{mac},{int(rng.integers(-70, -40))},11,-97,2,30,6,{ts_us % 2**32},47,2,{N_RAW},0,"
            f"\"[{data}]\"")


# ----------------------------------------------------------------------------------------- line / ceilings
def stage_line():
    rng = np.random.default_rng(0)
    lines = [make_line(rng, i, mac_of(1), 1_000_000 + i * 25_000) for i in range(2000)]
    nbytes = np.array([len(l) + 1 for l in lines])  # + '\n'
    fwd = [len("RX,10:BD:A3:CC:85:BC,") + n for n in nbytes]
    mean, fmean = float(nbytes.mean()), float(np.mean(fwd))
    uart_Bps = 921600 / 10   # 8N1: 10 bits per byte
    res = {
        "line_bytes_mean": mean, "line_bytes_p95": float(np.percentile(nbytes, 95)),
        "gateway_line_bytes_mean": fmean,
        "uart_921600_bytes_per_s": uart_Bps,
        "uart_lines_per_s_ceiling": uart_Bps / mean,
        "uart_senders_at_40hz_per_wired_receiver": uart_Bps / mean / RATE_HZ,
        "example_line": lines[0][:120] + "...",
    }
    rows = []
    for n in NS:
        links = n * n
        links_tx = n * (n - 1)
        rows.append({"N": n, "links_NxN": links, "links_N(N-1)": links_tx,
                     "per_receiver_KBps_NxN": n * RATE_HZ * mean / 1e3,
                     "per_receiver_uart_util_NxN": n * RATE_HZ * mean / uart_Bps,
                     "gateway_total_KBps_NxN": links * RATE_HZ * fmean / 1e3,
                     "gateway_total_KBps_N(N-1)": links_tx * RATE_HZ * fmean / 1e3,
                     "gateway_lines_per_s_NxN": links * RATE_HZ})
    res["by_N"] = rows
    OUT["line"] = res
    print(json.dumps(res, indent=1))
    return lines


# ----------------------------------------------------------------------------------------- parser
def stage_parser():
    rng = np.random.default_rng(1)
    lines = [make_line(rng, i, mac_of(i % 4), 1_000_000 + i * 25_000) for i in range(20000)]
    p = CSIParser()
    p.feed(C5_HEADER)
    for l in lines[:200]:
        p.feed(l, 0.0)
    t0 = time.perf_counter()
    for i, l in enumerate(lines):
        p.feed(l, float(i))
    dt = time.perf_counter() - t0
    per = dt / len(lines)
    # alternative int parse for headroom comparison (not repo code)
    datas = [l[l.find("[") + 1:l.rfind("]")] for l in lines]
    t0 = time.perf_counter()
    for s in datas:
        np.array(s.replace(",", " ").split(), dtype=np.int64)
    split_cost = (time.perf_counter() - t0) / len(datas)
    t0 = time.perf_counter()
    for s in datas:
        np.fromstring(s, dtype=np.int16, sep=",")
    fromstring_cost = (time.perf_counter() - t0) / len(datas)
    res = {"parser_feed_us_per_line": per * 1e6, "parser_lines_per_s_one_core": 1 / per,
           "of_which_split_to_numpy_us": split_cost * 1e6, "np_fromstring_alt_us": fromstring_cost * 1e6,
           "parsed": p.stats.parsed, "malformed": p.stats.malformed}
    OUT["parser"] = res
    print(json.dumps(res, indent=1))


# ----------------------------------------------------------------------------------------- serial reader
class FakePort:
    """Stands in for a pyserial port: read(n) returns up to n bytes from an in-memory buffer."""
    def __init__(self, data: bytes, chunk: int):
        self._mv = memoryview(data)
        self._pos = 0
        self._chunk = chunk
    @property
    def in_waiting(self):
        return min(self._chunk, len(self._mv) - self._pos)
    def read(self, n=1):
        b = self._mv[self._pos:self._pos + n].tobytes()
        self._pos += len(b)
        return b


def stage_reader():
    import serial.serialutil as su
    rng = np.random.default_rng(2)
    lines = [make_line(rng, i, mac_of(1), i * 25_000) for i in range(3000)]
    data = ("\n".join(lines) + "\n").encode()

    # pyserial's own read_until (what Serial.readline() runs): one self.read(1) per byte
    class P(FakePort):
        read_until = su.SerialBase.read_until
        readline = su.SerialBase.read_until
    port = P(data, 4096)
    port._timeout = None
    t0 = time.perf_counter()
    n = 0
    while True:
        raw = port.readline()
        if not raw:
            break
        raw.decode("utf-8", errors="replace")
        n += 1
    readline_per = (time.perf_counter() - t0) / n

    # chunked reader, same logic as csi/parser.py serial_stream (in_waiting chunks, split on \n)
    for chunk in (64, 512, 4096):
        port = FakePort(data, chunk)
        pending = b""
        m = 0
        t0 = time.perf_counter()
        while True:
            c = port.read(port.in_waiting or 1)
            if not c:
                break
            *ls, pending = (pending + c).split(b"\n")
            for raw in ls:
                raw.rstrip(b"\r").decode("utf-8", errors="replace")
                m += 1
        OUT.setdefault("reader", {})[f"chunked_{chunk}B_us_per_line"] = (time.perf_counter() - t0) / m * 1e6
    OUT["reader"]["readline_python_only_us_per_line"] = readline_per * 1e6
    OUT["reader"]["bytes_per_line"] = len(data) / len(lines)
    OUT["reader"]["note"] = ("python-level cost only; a real port adds one read() syscall per byte for readline "
                             "(~850 per line) vs one per chunk for the chunked reader")
    print(json.dumps(OUT["reader"], indent=1))


# ----------------------------------------------------------------------------------------- gateway
def stage_gateway():
    from csi.gateway import split_gateway
    res = []
    for n in (2, 4, 8):
        rng = np.random.default_rng(3)
        serials = [f"10:BD:A3:CC:85:{i:02X}" for i in range(n)]
        base = [make_line(rng, i, mac_of(i % n), i * 25_000) for i in range(400)]
        total = 40000
        src = [(float(i), f"RX,{serials[i % n]},{base[i % 400]}") for i in range(total)]
        streams = split_gateway(iter(src), {s: f"rx{i}" for i, s in enumerate(serials)})
        got = [0] * n
        def consume(k, s):
            for _, line in s:
                if line is not None:
                    got[k] += 1
        t0 = time.perf_counter(); c0 = time.process_time()
        ths = [threading.Thread(target=consume, args=(k, s)) for k, s in enumerate(streams.values())]
        for t in ths: t.start()
        for t in ths: t.join()
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        res.append({"receivers": n, "lines": total, "delivered": sum(got), "dropped_queue_full": total - sum(got),
                    "wall_us_per_line": wall / total * 1e6, "cpu_us_per_line": cpu / total * 1e6})
    OUT["gateway"] = res
    print(json.dumps(res, indent=1))


# ----------------------------------------------------------------------------------------- pipeline
def packet_schedule(n_rx, n_tx, seconds, t0=1_700_000_000.0, seed=4):
    """Per-link packets in host-time order: each sender at 40 Hz with jitter, heard by every receiver."""
    rng = np.random.default_rng(seed)
    base = {(r, s): rng.uniform(15, 40, N_KEPT).astype(np.float32) for r in range(n_rx) for s in range(n_tx)}
    n_pk = int(seconds * RATE_HZ)
    events = []
    for s in range(n_tx):
        phase = rng.uniform(0, 1 / RATE_HZ)
        for k in range(n_pk):
            t = phase + k / RATE_HZ
            for r in range(n_rx):
                events.append((t + rng.uniform(0, 0.002), r, s, k))
    events.sort()
    for t, r, s, k in events:
        amp = base[(r, s)] + rng.normal(0, 0.5, N_KEPT).astype(np.float32)
        yield f"rx{r}", CSIPacket(host_ts=t0 + t, device_ts=int(1_000_000 + t * 1e6) + r, rssi=-55, channel=6,
                                  amplitude=amp, mac=mac_of(s))


def run_pipeline(n, mode, warm_s, meas_s, profile=None):
    room = make_room(n, n)
    thr = {f"rx{r}<-{mac_of(s)[-2:]}": 0.5 for r in range(n) for s in range(n)} if mode == "fixed" else None
    st = DashboardState(room, thr, mode=mode)
    allp = list(packet_schedule(n, n, warm_s + meas_s))
    cut = 1_700_000_000.0 + warm_s
    meas = [x for x in allp if x[1].host_ts >= cut]
    for rx, p in allp[:len(allp) - len(meas)]:
        st.push(rx, p)
    del allp
    if profile is not None:
        profile.enable()
    push_t = snap_t = 0.0
    frames = 0
    next_frame = meas[0][1].host_ts + 0.2
    payload_len = 0
    t0 = time.perf_counter()
    for rx, p in meas:
        a = time.perf_counter()
        st.push(rx, p)
        b = time.perf_counter()
        push_t += b - a
        if p.host_ts >= next_frame:   # the server emits a frame every 0.2 s of packet time, under the lock
            text = json.dumps(st.snapshot(p.host_ts))
            snap_t += time.perf_counter() - b
            payload_len = len(text)
            frames += 1
            next_frame = p.host_ts + 0.2
    total = time.perf_counter() - t0
    if profile is not None:
        profile.disable()
    npk = len(meas)
    return {"N": n, "links": n * n, "mode": mode, "packets": npk, "offered_pkt_per_s": n * n * RATE_HZ,
            "push_us_per_pkt": push_t / npk * 1e6, "frame_ms": snap_t / max(frames, 1) * 1e3,
            "frame_payload_KB": payload_len / 1e3, "sse_KBps_at_5Hz": payload_len * 5 / 1e3,
            "total_us_per_pkt": total / npk * 1e6,
            "cpu_s_per_s_of_traffic": total / meas_s,
            "max_pkt_per_s_one_core": npk / total}


def stage_pipeline(modes=("fixed", "auto")):
    res = []
    for mode in modes:
        for n in NS:
            warm = 80.0 if mode == "auto" else 65.0   # past the heatmap's 60 s baseline and auto's 30 s cap / 75 s window
            if n >= 10:
                warm = 70.0 if mode == "auto" else 62.0
            r = run_pipeline(n, mode, warm, 5.0)
            res.append(r)
            print(json.dumps(r), flush=True)
    OUT["pipeline"] = res


# ----------------------------------------------------------------------------------------- end-to-end server
def stage_server():
    from csi.server import DashboardServer
    res = []
    for n in (2, 4, 6, 8, 10):
        room = make_room(n, n)
        seconds = 20.0
        rng = np.random.default_rng(6)
        texts = [make_line(rng, i, mac_of(0), 0)[0:0] or None for i in range(1)]  # noqa (placeholder)
        # pre-render lines per receiver (rendering is not host cost)
        per_rx = {f"rx{r}": [] for r in range(n)}
        pool = {}
        for rx, p in packet_schedule(n, n, seconds, seed=7):
            key = (rx, p.mac)
            if key not in pool:
                pool[key] = [make_line(rng, 0, p.mac, 0) for _ in range(8)]
            src = pool[key][len(per_rx[rx]) % 8]
            # patch seq/timestamp fields cheaply: rebuild the prefix
            pre, data = src.split('"[', 1)
            f = pre.split(",")
            f[9] = str(p.device_ts % 2**32)
            per_rx[rx].append((p.host_ts, ",".join(f) + '"[' + data))
        thr = {f"rx{r}<-{mac_of(s)[-2:]}": 0.5 for r in range(n) for s in range(n)}
        sources = {rx: iter([(0.0, C5_HEADER)] + v) for rx, v in per_rx.items()}
        total = sum(len(v) for v in per_rx.values())
        srv = DashboardServer(room, sources, thr, host="127.0.0.1", port=0)
        c0 = time.process_time(); t0 = time.perf_counter()
        srv.start()
        ok = srv.wait_finished(timeout=600)
        wall = time.perf_counter() - t0; cpu = time.process_time() - c0
        srv.stop()
        r = {"N": n, "links": n * n, "packets": total, "finished": ok, "wall_s": wall, "cpu_s": cpu,
             "pkts_per_s_wall": total / wall, "offered_pkt_per_s": n * n * RATE_HZ,
             "realtime_factor": (total / wall) / (n * n * RATE_HZ)}
        res.append(r)
        print(json.dumps(r), flush=True)
    OUT["server"] = res


def stage_profile():
    pr = cProfile.Profile()
    r = run_pipeline(8, "fixed", 62.0, 5.0, profile=pr)
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(22)
    txt = s.getvalue()
    pr2 = cProfile.Profile()
    run_pipeline(8, "auto", 70.0, 5.0, profile=pr2)
    s2 = io.StringIO()
    pstats.Stats(pr2, stream=s2).sort_stats("tottime").print_stats(22)
    OUT["profile"] = {"fixed_N8": txt, "auto_N8": s2.getvalue()}
    print(txt)
    print(s2.getvalue())


if __name__ == "__main__":
    stages = sys.argv[1:] or ["line", "parser", "reader", "gateway", "pipeline", "server", "profile"]
    for s in stages:
        print(f"===== {s}", flush=True)
        globals()[f"stage_{s}"]()
    out = f"bench_results_{'_'.join(stages)}.json"
    with open(out, "w") as f:
        json.dump(OUT, f, indent=1, default=str)
    print("wrote", out)
