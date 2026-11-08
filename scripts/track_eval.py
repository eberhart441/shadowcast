"""Score the motion track against the simulator's known walk. No hardware.

    python scripts/track_eval.py                   # default room, 3 laps of the demo walk, plus an empty room
    python scripts/track_eval.py --laps 5 --rate 37

Feeds simulated packets through DashboardState and samples the payload at 5 Hz, like the server. For each
sample it compares three estimates with the true position: the hottest heatmap cell, the raw blob centroid
(the tracker's measurement), and the Kalman track. Reports how often each exists, the median and 90th
percentile error, and the jitter (mean distance moved between samples; the true walk moves speed / 5 Hz).
The empty-room run reports how often a track appears with nobody there.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csi.dashboard import DashboardState  # noqa: E402
from csi.heatmap import cell_centres  # noqa: E402
from csi.room import load_room  # noqa: E402
from csi.server import default_walk, simulated_thresholds, walk_period_s  # noqa: E402
from csi.simulate import simulate_room_links  # noqa: E402
from csi.tracker import heat_measurement  # noqa: E402

T0 = 1_700_000_000.0
SAMPLE_S = 0.2
WARMUP_S = 15.0   # heatmap baselines need 10 s; skip the start


def run(room, person, thresholds, duration_s, rate_hz, seed):
    """Per sample: (t, truth or None, peak, centroid, track), each an (x, y) or None."""
    state = DashboardState(room, thresholds)
    centres = None
    out, next_t = [], T0 + SAMPLE_S
    for receiver, pkt in simulate_room_links(person=person, room=room, duration_s=duration_s, rate_hz=rate_hz,
                                             seed=seed, t0=T0):
        state.push(receiver, pkt)
        if pkt.host_ts < next_t:
            continue
        next_t += SAMPLE_S
        snap = state.snapshot(pkt.host_ts)
        heat = snap["heatmap"]
        if centres is None:
            centres = cell_centres(heat["cols"], heat["rows"], room.width_m, room.depth_m)
        cells = np.asarray(heat["cells"])
        peak = tuple(centres[int(cells.argmax())]) if cells.max() > 0 else None
        m = heat_measurement(cells, centres)
        centroid = None if m is None else m[:2]
        track = snap["track"]
        t = pkt.host_ts - T0
        truth = None if person is None else tuple(person.position(np.array([t]))[0])
        out.append((t, truth, peak, centroid, None if track is None else (track["x"], track["y"])))
    return [row for row in out if row[0] >= WARMUP_S]


def score(rows, idx):
    pts = [(r[1], r[idx]) for r in rows]
    have = [(t, e) for t, e in pts if e is not None]
    if not have:
        return "never"
    err = np.array([np.hypot(t[0] - e[0], t[1] - e[1]) for t, e in have])
    est = [e for _, e in pts]
    steps = [np.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(est, est[1:]) if a is not None and b is not None]
    return (f"exists {100 * len(have) / len(pts):5.1f}%   error median {np.median(err):.2f} m  "
            f"p90 {np.percentile(err, 90):.2f} m   jitter {np.mean(steps):.3f} m/sample")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--laps", type=float, default=3)
    ap.add_argument("--rate", type=float, default=40.0, help="packets/s per link (the rig runs about 37)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    room = load_room()
    walk = default_walk(room)
    thresholds = simulated_thresholds(room)
    duration = WARMUP_S + a.laps * walk_period_s(walk)
    print(f"room {room.width_m} x {room.depth_m} m, {len(room.links())} links, walk {walk.speed_mps} m/s "
          f"(true motion {walk.speed_mps * SAMPLE_S:.3f} m/sample), {duration:.0f} s at {a.rate:g} Hz\n")

    rows = run(room, walk, thresholds, duration, a.rate, a.seed)
    for name, idx in (("hottest cell ", 2), ("blob centroid", 3), ("kalman track ", 4)):
        print(f"{name}  {score(rows, idx)}")

    empty = run(room, None, thresholds, 60.0, a.rate, a.seed + 100)
    false = sum(r[4] is not None for r in empty)
    print(f"\nempty room: track present in {false}/{len(empty)} samples ({100 * false / len(empty):.1f}%)")


if __name__ == "__main__":
    main()
