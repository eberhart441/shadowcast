"""Score a guided accuracy run: replay its raw CSI through the dashboard pipeline and compare with the prompts.

    python scripts/accuracy_eval.py runs/accuracy-1               # the report
    python scripts/accuracy_eval.py runs/accuracy-1 --variants    # also other tracker settings, frozen baseline

The run folder holds raw.tsv (dashboard --raw-log), labels.jsonl, live.jsonl and thresholds.json
(scripts/accuracy_run.py). The replay uses the same DashboardState, thresholds and 0.2 s frames as the
server, so its dot is the dashboard's dot; the live log checks that. Times: labels are laptop time, the raw
log is Pi time; the offset comes from live.jsonl (each /api/state carries the Pi's time) or --offset.

Accuracy is bias: how far the dot's average sits from the mark. Precision is spread: how far the dot wanders
around its own average (CEP50 and CEP95: radii holding half and 95% of the samples). Both are only as good as
the truth: a taped mark and a person moving on the spot are good to about csi/accuracy.MARK_TOLERANCE_M.
Writes report.json and accuracy_map.json (per mark, for the dashboard) into the run folder.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csi import accuracy as A  # noqa: E402
from csi.dashboard import DashboardState  # noqa: E402
from csi.parser import CSIParser, parse_stream  # noqa: E402
from csi.rawlog import read_raw  # noqa: E402
from csi.room import load_room  # noqa: E402
from csi.tracker import Tracker, heat_measurement  # noqa: E402

FRAME_S = 0.2   # the server's frame interval


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def clock_offset(live: list[dict]) -> tuple[Optional[float], dict]:
    """Laptop time minus Pi time, from /api/state samples (each carries the Pi's time_s).

    Uses only the reply leg: t1 (laptop, reply received) - time_s (Pi, reply built) is the offset plus the reply's
    travel time, which is short. The request leg is not: through the SSH tunnel a request can wait seconds for its
    channel to open, so the midpoint of t0 and t1 was off by a second on the rig (checked against SSH `date`
    round trips of 3 ms: -0.031 s, against -0.028 s from this and -1.048 s from the midpoint).
    """
    rows = [r for r in live if r.get("time_s") and r.get("t1")]
    if not rows:
        return None, {}
    reply = np.array([r["t1"] - r["time_s"] for r in rows])
    rtt = np.array([r["t1"] - r["t0"] for r in rows])
    return float(np.percentile(reply, 5)), {"samples": len(rows), "reply_leg_median_s": float(np.median(reply)),
                                             "rtt_median_s": float(np.median(rtt))}


# --------------------------------------------------------------------------- replay

def replay(raw: Path, room, thresholds: dict, t_from: float, t_to: float, freeze_after: Optional[float] = None,
           ref_window: Optional[tuple[float, float]] = None, accuracy_map: Optional[dict] = None):
    """Frames (every 0.2 s of host time) and heat targets (every heatmap step) of the dashboard pipeline.

    ``ref_window`` (host times): an empty-room stretch the locator learns its reference from, as the dashboard
    does from its calibration clips. Like live, the reference takes effect when that stretch ends."""
    streams = read_raw(raw, t_from, t_to)

    def tag(name, lines):   # a function, so each generator keeps its own receiver name
        return ((pkt.host_ts, name, pkt) for pkt in parse_stream(iter(lines), CSIParser()))
    tagged = [tag(name, lines) for name, lines in streams.items()]
    state = DashboardState(room, thresholds)
    if accuracy_map is not None:
        state.locator.set_accuracy_map(accuracy_map)
    clip = None
    if ref_window is not None:
        ref = read_raw(raw, *ref_window)
        clip = sorted(((pkt.host_ts, name, pkt) for name, lines in ref.items()
                       for pkt in parse_stream(iter(lines), CSIParser())), key=lambda item: item[0])
        clip = [(name, pkt) for _, name, pkt in clip]
    heat = state._heat
    if freeze_after is not None:   # a baseline learned only before freeze_after (the empty room), then held
        orig = heat._update_baseline

        def frozen(label, now, row, orig=orig):
            return heat._baselines[label].value() if now >= freeze_after else orig(label, now, row)
        heat._update_baseline = frozen
    frames, targets, next_frame, last_heat = [], [], None, None
    for ts, name, pkt in heapq.merge(*tagged, key=lambda item: item[0]):
        if clip is not None and ts >= ref_window[1]:
            state.locator.reference_from_clips([clip])
            clip = None
        state.push(name, pkt)
        if heat._last_t != last_heat:
            last_heat = heat._last_t
            targets.append((last_heat, heat.target.copy()))
        if next_frame is None:
            next_frame = ts + FRAME_S
        elif ts >= next_frame:
            snap = state.snapshot(ts)
            m = heat_measurement(heat.target, heat.centres)
            tr = snap["track"]
            loc = snap.get("location") or {}
            frames.append({
                "t": ts,
                "location": (loc["x"], loc["y"]) if loc.get("state") == "moving" else None,
                "loc_state": loc.get("state"),
                "loc_level": loc.get("level"),
                "loc_sigma": loc.get("sigma"),
                "loc_ghost": loc.get("ghost"),
                "track": None if tr is None else (tr["x"], tr["y"]),
                "conf": None if tr is None else tr["confidence"],
                "centroid": None if m is None else (m[0], m[1]),
                "hottest": tuple(heat.centres[int(np.argmax(heat.target))]) if heat.target.max() > 0 else None,
                "heat_max": float(heat.target.max()),
                "links": {lk["id"]: (lk["state"], lk["pkt_rate"], lk["evidence"]) for lk in snap["links"]},
            })
            next_frame = ts + FRAME_S
    return frames, targets, heat.centres


# --------------------------------------------------------------------------- metrics

def nearest_mark(p) -> int:
    return min(A.MARKS, key=lambda k: (A.MARKS[k][0] - p[0]) ** 2 + (A.MARKS[k][1] - p[1]) ** 2)


def in_window(frames, seg):
    return [f for f in frames if seg.t0 <= f["t"] < seg.t1]


def static_metrics(rows, truth, mark, key="track") -> dict:
    est = [r[key] for r in rows if r[key] is not None]
    out = {"frames": len(rows), "available": round(len(est) / len(rows), 3) if rows else 0.0}
    if len(est) < 3:
        return out
    E = np.array(est)
    mean = E.mean(axis=0)
    d_mean = np.linalg.norm(E - mean, axis=1)
    err = np.linalg.norm(E - np.array(truth), axis=1)
    zone = [nearest_mark(p) for p in E]
    out.update({
        "mean": [round(float(mean[0]), 3), round(float(mean[1]), 3)],
        "bias_m": round(float(np.linalg.norm(mean - np.array(truth))), 3),
        "bias_xy": [round(float(mean[0] - truth[0]), 3), round(float(mean[1] - truth[1]), 3)],
        "cep50_m": round(float(np.percentile(d_mean, 50)), 3),
        "cep95_m": round(float(np.percentile(d_mean, 95)), 3),
        "std_xy": [round(float(E[:, 0].std()), 3), round(float(E[:, 1].std()), 3)],
        "err_median_m": round(float(np.median(err)), 3),
        "err_p68_m": round(float(np.percentile(err, 68)), 3),
        "err_p90_m": round(float(np.percentile(err, 90)), 3),
        "zone_correct": round(sum(z == mark for z in zone) / len(zone), 3),
        "zones": {str(k): zone.count(k) for k in sorted(set(zone))},
    })
    if mark != 5:
        tw = np.array(A.MARKS[A.twin(mark)])
        out["twin_confused"] = round(float(np.mean(np.linalg.norm(E - tw, axis=1) < err)), 3)
    return out


def walk_metrics(rows, seg, key="track") -> dict:
    est = [(r["t"], r[key]) for r in rows if r[key] is not None]
    out = {"frames": len(rows), "available": round(len(est) / len(rows), 3) if rows else 0.0}
    if len(est) < 10:
        return out
    T = np.array([t for t, _ in est])
    E = np.array([p for _, p in est])

    def err_at(shift):
        truth = np.array([seg.truth(t - shift) for t in T])
        return np.linalg.norm(E - truth, axis=1)
    e0 = err_at(0.0)
    shifts = np.arange(0, 3.01, 0.05)
    meds = [float(np.median(err_at(s))) for s in shifts]
    best = int(np.argmin(meds))
    steps = np.linalg.norm(np.diff(E, axis=0), axis=1)
    out.update({"err_median_m": round(float(np.median(e0)), 3), "err_p90_m": round(float(np.percentile(e0, 90)), 3),
                "lag_s": round(float(shifts[best]), 2), "err_median_at_lag_m": round(meds[best], 3),
                "jitter_m_per_frame": round(float(np.median(steps)), 3)})
    return out


def empty_metrics(rows, key="track") -> dict:
    if not rows:
        return {"frames": 0}
    track = np.array([r[key] is not None for r in rows])
    hm = np.array([r["heat_max"] for r in rows])
    motion = np.mean([any(v[0] == "MOTION" for v in r["links"].values()) for r in rows])
    return {"frames": len(rows), "false_track": round(float(track.mean()), 3),
            "heat_max_p50": round(float(np.median(hm)), 3), "heat_max_p95": round(float(np.percentile(hm, 95)), 3),
            "any_link_motion": round(float(motion), 3)}


def still_metrics(rows, seg, key="track") -> dict:
    if not rows:
        return {"frames": 0}
    have = [(r["t"] - seg.t0, r[key] is not None) for r in rows]
    lost_at = next((t for t, h in have if not h), None)
    return {"frames": len(rows), "available": round(sum(h for _, h in have) / len(have), 3),
            "dot_lost_after_s": None if lost_at is None else round(lost_at, 1)}


def link_health(rows) -> dict:
    out = {}
    for link in sorted({k for r in rows for k in r["links"]}):
        vals = [r["links"][link] for r in rows if link in r["links"]]
        rates = [v[1] or 0.0 for v in vals]
        states = [v[0] for v in vals]
        out[link] = {"rate_median": round(float(np.median(rates)), 1), "rate_min": round(float(np.min(rates)), 1),
                     "not_scoring": round(sum(s not in ("EMPTY", "MOTION") for s in states) / len(states), 3)}
    return out


def score(frames, segs, key="track") -> dict:
    out = {"marks": {}, "empty": [], "still": None, "move_again": None, "walk": None, "health": {}}
    for seg in segs:
        rows = in_window(frames, seg)
        name = f"{seg.kind}{seg.mark or ''}{'_final' if seg.meta.get('final') else ''}"
        out["health"][name] = link_health(rows) if rows else {}
        if seg.kind == "hold":
            out["marks"][seg.mark] = static_metrics(rows, A.MARKS[seg.mark], seg.mark, key)
        elif seg.kind == "empty":
            out["empty"].append(dict(empty_metrics(rows, key), final=seg.meta.get("final", False)))
        elif seg.kind == "still":
            out["still"] = still_metrics(rows, seg, key)
        elif seg.kind == "move_again":
            out["move_again"] = static_metrics(rows, A.MARKS[5], 5, key)
        elif seg.kind == "walk":
            out["walk"] = walk_metrics(rows, seg, key)
    marks = [m for m in out["marks"].values() if "bias_m" in m]
    if marks:
        out["summary"] = {
            "bias_median_m": round(float(np.median([m["bias_m"] for m in marks])), 3),
            "bias_max_m": round(float(np.max([m["bias_m"] for m in marks])), 3),
            "cep95_median_m": round(float(np.median([m["cep95_m"] for m in marks])), 3),
            "err_median_m": round(float(np.median([m["err_median_m"] for m in marks])), 3),
            "zone_correct": round(float(np.mean([m["zone_correct"] for m in marks])), 3),
            "twin_confused": round(float(np.mean([m["twin_confused"] for m in marks if "twin_confused" in m])), 3),
            "available": round(float(np.mean([m["available"] for m in out["marks"].values()])), 3),
        }
    return out


def error_given_estimate(frames, segs, room, cols: int = 14, rows: int = 9, kernel_m: float = 0.3) -> dict:
    """How wrong is the dot when it is shown here? Over every hold frame, the 68th percentile of the error,
    weighted by how close each frame's estimate is to each grid cell. Conditioned on the estimate, not the truth:
    a wrong estimate often lands where the system is otherwise good, and must not inherit that confidence."""
    pairs = []
    for s in segs:
        if s.kind == "hold":
            for f in in_window(frames, s):
                if f["location"] is not None:
                    pairs.append((*f["location"], float(np.hypot(f["location"][0] - A.MARKS[s.mark][0],
                                                                  f["location"][1] - A.MARKS[s.mark][1]))))
    if not pairs:
        return {}
    P = np.array(pairs)
    xs = (np.arange(cols) + 0.5) * room.width_m / cols
    ys = (np.arange(rows) + 0.5) * room.depth_m / rows
    overall = float(np.percentile(P[:, 2], 68))
    grid = []
    for y in ys:
        row = []
        for x in xs:
            w = np.exp(-((P[:, 0] - x) ** 2 + (P[:, 1] - y) ** 2) / (2 * kernel_m ** 2))
            if w.sum() < 5:              # too few estimates landed here: fall back to the whole room
                row.append(round(overall, 3))
                continue
            order = np.argsort(P[:, 2])
            cum = np.cumsum(w[order]) / w.sum()
            row.append(round(float(P[order, 2][np.searchsorted(cum, 0.68)]), 3))
        grid.append(row)
    return {"cols": cols, "rows": rows, "p68_m": grid, "overall_p68_m": round(overall, 3), "frames": len(P)}


def locator_extras(frames, segs) -> dict:
    """Presence per segment kind, and how often the true position falls inside the reported 1-sigma ellipse."""
    out = {"presence": {}, "ellipse": None}
    for kind in ("empty", "hold", "still", "move_again", "walk"):
        rows = [f for s in segs if s.kind == kind for f in in_window(frames, s)]
        if rows:
            states = [f["loc_state"] for f in rows]
            out["presence"][kind] = {st: round(states.count(st) / len(states), 3) for st in sorted(set(map(str, states)))}
    inside, total, ghosts = 0, 0, 0
    for s in segs:
        if s.kind != "hold":
            continue
        tx, ty = A.MARKS[s.mark]
        for f in in_window(frames, s):
            if f["location"] is None or not f["loc_sigma"]:
                continue
            sg = f["loc_sigma"]
            a = np.radians(sg["angle_deg"])
            dx, dy = tx - f["location"][0], ty - f["location"][1]
            u = dx * np.cos(a) + dy * np.sin(a)
            v = -dx * np.sin(a) + dy * np.cos(a)
            inside += (u / sg["major_m"]) ** 2 + (v / sg["minor_m"]) ** 2 <= 1
            total += 1
            ghosts += f["loc_ghost"] is not None
    if total:
        out["ellipse"] = {"truth_inside_1sigma": round(inside / total, 3), "frames": total,
                          "ghost_shown": round(ghosts / total, 3)}
    return out


# --------------------------------------------------------------------------- live check, variants

def live_vs_replay(live, frames, offset) -> dict:
    """Does the replay reproduce the dot the dashboard showed? Compares live /api/state with the nearest frame."""
    ts = np.array([f["t"] for f in frames])
    both, agree, d = 0, 0, []
    for r in live:
        if not r.get("time_s"):
            continue
        i = int(np.searchsorted(ts, r["time_s"]))
        if i <= 0 or i >= len(ts):
            continue
        f = frames[i - 1] if abs(ts[i - 1] - r["time_s"]) < abs(ts[i] - r["time_s"]) else frames[i]
        lv = r.get("track")
        both += 1
        agree += (lv is None) == (f["track"] is None)
        if lv is not None and f["track"] is not None:
            d.append(np.hypot(lv["x"] - f["track"][0], lv["y"] - f["track"][1]))
    return {"compared": both, "presence_agree": round(agree / both, 3) if both else None,
            "position_diff_median_m": round(float(np.median(d)), 3) if d else None,
            "position_diff_p90_m": round(float(np.percentile(d, 90)), 3) if d else None}


def tracker_frames(targets, centres, room, **kw):
    """Re-run only the tracker over recorded heat targets, sampled like the server (every 0.2 s)."""
    tr = Tracker(room.width_m, room.depth_m, centres, **kw)
    frames, next_frame = [], None
    for t, cells in targets:
        tr.update(t, cells)
        if next_frame is None or t >= next_frame:
            s = tr.state(t)
            frames.append({"t": t, "track": None if s is None else (s["x"], s["y"]), "links": {}, "heat_max": 0.0})
            next_frame = t + FRAME_S
    return frames


def variants(targets, centres, room, segs) -> list[dict]:
    grid = itertools.product((0.08, 0.15, 0.25, 0.4), (1.5, 4.0, 8.0), (0.5, 1.5), (0.05, 0.1, 0.2), (0.3, 0.45, 0.6))
    rows = []
    for ms, acc, vt, mh, hf in grid:
        kw = dict(meas_sigma_m=ms, accel_sigma=acc, velocity_tau_s=vt, min_heat=mh, hot_fraction=hf)
        sc = score(tracker_frames(targets, centres, room, **kw), segs)
        s, w = sc.get("summary", {}), sc.get("walk") or {}
        e = [x["false_track"] for x in sc["empty"]]
        rows.append({**kw, **{k: s.get(k) for k in ("bias_median_m", "cep95_median_m", "err_median_m",
                                                    "zone_correct", "available")},
                     "walk_err_m": w.get("err_median_m"), "walk_lag_s": w.get("lag_s"),
                     "empty_false": round(float(np.mean(e)), 3) if e else None})
    return rows


# --------------------------------------------------------------------------- report

def print_report(sc: dict, title: str) -> None:
    print(f"\n=== {title}")
    if "summary" in sc:
        s = sc["summary"]
        print(f"marks: bias median {s['bias_median_m']} m (max {s['bias_max_m']}), spread CEP95 median "
              f"{s['cep95_median_m']} m, error median {s['err_median_m']} m, right zone {100*s['zone_correct']:.0f}%, "
              f"mirror-twin confused {100*s['twin_confused']:.0f}%, dot present {100*s['available']:.0f}%")
    print(" mark  truth          mean           bias   bias(x,y)        CEP50  CEP95  err50  err90  zone  twin  avail")
    for k, m in sorted(sc["marks"].items()):
        if "bias_m" not in m:
            print(f"  {k}    {A.MARKS[k]}  dot present only {100*m['available']:.0f}%")
            continue
        twin = f"{100 * m['twin_confused']:3.0f}%" if "twin_confused" in m else "   - "
        print(f"  {k}    ({A.MARKS[k][0]:.2f},{A.MARKS[k][1]:.2f})  ({m['mean'][0]:.2f},{m['mean'][1]:.2f})  "
              f"{m['bias_m']:.2f}   ({m['bias_xy'][0]:+.2f},{m['bias_xy'][1]:+.2f})   {m['cep50_m']:.2f}   "
              f"{m['cep95_m']:.2f}   {m['err_median_m']:.2f}   {m['err_p90_m']:.2f}   {100*m['zone_correct']:3.0f}%  "
              f"{twin}  {100*m['available']:3.0f}%")
    if sc.get("walk"):
        print(f"walk: {sc['walk']}")
    for e in sc["empty"]:
        print(f"empty{' (end)' if e.get('final') else ''}: {e}")
    if sc.get("still"):
        print(f"still: {sc['still']}   move again: "
              f"{ {k: sc['move_again'].get(k) for k in ('available', 'bias_m', 'cep95_m')} if sc.get('move_again') else None}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    ap.add_argument("--room", type=Path, default=None, help="room config the run used (default: run/room.json, "
                                                             "else config/room.json)")
    ap.add_argument("--offset", type=float, default=None, help="laptop minus Pi time, s (default: from live.jsonl)")
    ap.add_argument("--variants", action="store_true")
    ap.add_argument("--accuracy-map", type=Path, default=None,
                    help="load this measured accuracy map into the locator (checks the reported uncertainty)")
    a = ap.parse_args()

    labels = load_jsonl(a.run / "labels.jsonl")
    live = load_jsonl(a.run / "live.jsonl")
    thresholds = json.loads((a.run / "thresholds.json").read_text())
    room_path = a.room or (a.run / "room.json" if (a.run / "room.json").exists() else None)
    room = load_room(room_path) if room_path else load_room()
    offset, off_info = (a.offset, {"given": True}) if a.offset is not None else clock_offset(live)
    if offset is None:
        raise SystemExit("no clock offset: live.jsonl has no samples; pass --offset")
    segs = A.segments_from_labels(labels, offset)
    t_from, t_to = min(s.t0 for s in segs) - 70, max(s.t1 for s in segs) + 5
    print(f"clock offset (laptop - Pi) {offset:+.3f} s {off_info}; {len(segs)} segments; "
          f"thresholds {sorted(thresholds)}")

    cal = [e for e in labels if e["event"] in ("calibration_start", "calibration_end")]
    ref_window = None
    starts = [e["t"] - offset for e in cal if e["event"] == "calibration_start"]
    ends = [e["t"] - offset for e in cal if e["event"] == "calibration_end"]
    if starts and ends:   # the last calibration's recording: lead-in over, before its end
        s0 = max(starts)
        e0 = min(t for t in ends if t > s0)
        ref_window = (s0 + 4.0, e0 - 1.0)
    acc = json.loads(a.accuracy_map.read_text()) if a.accuracy_map else None
    frames, targets, centres = replay(a.run / "raw.tsv", room, thresholds, t_from, t_to, ref_window=ref_window,
                                      accuracy_map=acc)
    print(f"locator reference from {'the calibration window' if ref_window else 'nothing (no calibration labels)'}")
    report = {"offset_s": offset, "offset_info": off_info, "thresholds": thresholds,
              "live_vs_replay": live_vs_replay(live, frames, offset) if live else None}
    print("live vs replay:", report["live_vs_replay"])
    for key in ("location", "track", "centroid", "hottest"):
        report[key] = score(frames, segs, key)
        print_report(report[key], {"location": "LOCATOR (raw CSI, no high-pass; csi/locator.py)",
                                   "track": "Kalman dot on the heatmap (the old dot)", "centroid": "raw blob centroid",
                                   "hottest": "hottest cell"}[key])
        if key == "location":
            report["locator"] = locator_extras(frames, segs)
            print("locator presence by segment (share of frames per state):")
            for kind, shares in report["locator"]["presence"].items():
                print(f"  {kind:10s} {shares}")
            print(f"locator uncertainty: {report['locator']['ellipse']}")
    print("\nlink health per segment (median pkt/s, min, share not scoring):")
    for name, h in report["track"]["health"].items():
        bad = {k: v for k, v in h.items() if v["rate_min"] < 15 or v["not_scoring"] > 0.05}
        print(f"  {name:14s} " + "  ".join(f"{k}:{v['rate_median']:.0f}" for k, v in h.items())
              + (f"   ATTENTION {bad}" if bad else ""))

    if a.variants:
        empty_end = max((s.t1 for s in segs if s.kind == "empty" and not s.meta.get("final")), default=None)
        if empty_end is not None:
            f2, _, _ = replay(a.run / "raw.tsv", room, thresholds, t_from, t_to, freeze_after=empty_end)
            report["frozen_baseline"] = score(f2, segs)
            print_report(report["frozen_baseline"], "Kalman dot with the baseline frozen after the empty room")
        rows = variants(targets, centres, room, segs)
        report["variants"] = rows
        ok = [r for r in rows if r["bias_median_m"] is not None and (r["available"] or 0) > 0.8]
        print("\ntracker settings, best 10 by median error at the marks (dot present > 80%):")
        for r in sorted(ok, key=lambda r: (r["err_median_m"], r["cep95_median_m"]))[:10]:
            print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    (a.run / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (a.run / "accuracy_map.json").write_text(json.dumps({
        "note": f"measured on the accuracy run in {a.run.name}: the locator's error at each mark (in-sample)",
        "marks": {str(k): {"truth": A.MARKS[k], **{x: m.get(x) for x in ("mean", "bias_m", "cep50_m", "cep95_m",
                                                                            "err_median_m", "err_p68_m", "err_p90_m",
                                                                            "available", "twin_confused",
                                                                            "zone_correct")}}
                  for k, m in report["location"]["marks"].items()},
        "summary": report["location"].get("summary"), "walk": report["location"].get("walk"),
        "error_given_estimate": error_given_estimate(frames, segs, room)}, indent=1))
    print(f"\nwrote {a.run / 'report.json'} and {a.run / 'accuracy_map.json'}")


if __name__ == "__main__":
    main()
