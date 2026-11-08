"""scripts/tune_replay.py: the offline accuracy tuner (replays of logged runs with other heatmap and tracker settings).

The synthetic runs are written in the dashboard's --log format: a header with the room, then one payload per frame
with the link rows the heatmap reads (id, state, score, age_s). Four links span a 4 m x 3 m room. The room is quiet for
the first 20 s (every link EMPTY at score 1, so the quiet baselines are 1), then a walker goes along y = 0.8 m and back
at 0.5 m/s, and every link within 0.8 m of the walker reads 3x its quiet score, in MOTION. Some link is always that
close. Frames come every 0.25 s, which floating point holds exactly, so the walk and the empty label cover known frames.

The synthetic truth the tuner has to find: a ratio rule whose floor is below the walker's 3x hears the walker and
places it; a floor above 3x never gives evidence, so it never shows an estimate.
"""
import json
import math
import statistics
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tune_replay  # noqa: E402

T0 = 1_700_000_000.0
STEP = 0.25
W, D = 4.0, 3.0
RECEIVERS = {"rx1": (0.0, 0.0), "rx2": (4.0, 0.0)}
SENDERS = {"tx0": ((4.0, 3.0), "1a:0c:12:00:00:00"), "tx1": ((0.0, 3.0), "1a:0c:12:00:00:01")}
WALK = ((20.0, 0.5, 0.8), (26.0, 3.5, 0.8), (32.0, 0.5, 0.8))   # (s from the first frame, x, y): there and back
END_S = 40.0
RISE = 3.0                  # a raised link's score, in units of its quiet score
REACH_M = 0.8               # links this close to the walker are raised
WALK_FRAMES = 48            # 20 <= t < 32 s
EMPTY_FRAMES = 28           # the empty label, 12 <= t < 19 s
WALK_CSV = "t_s,x,y\n" + "".join(f"{t:g},{x:g},{y:g}\n" for t, x, y in WALK)
LABELS_CSV = "start_s,end_s,label\n12,19,empty\n20,32,walking\n"
DEAF = {"estimator": "backprojection", "evidence": "ratio", "r_floor": 4.0, "r_full": 8.0}
HEARING = {"estimator": "backprojection", "evidence": "ratio", "r_floor": 1.2, "r_full": 3.0}


def links():
    """label -> segment, for every (receiver, sender) pair, labelled as csi.links.LinkId labels them."""
    return {f"{rx}<-{mac[-2:]}": (a, b) for rx, a in RECEIVERS.items() for b, mac in SENDERS.values()}


def room_header(**extra):
    """The room as csi/server.py writes it into a log header (dataclasses.asdict of csi.room.Room)."""
    receivers = [{"name": name, "role": "receiver", "x": x, "y": y, "mac": "", "chip": "ESP32-C5"}
                 for name, (x, y) in RECEIVERS.items()]
    senders = [{"name": name, "role": "sender", "x": x, "y": y, "mac": mac, "chip": "ESP32-C5"}
               for name, ((x, y), mac) in SENDERS.items()]
    return {"width_m": W, "depth_m": D, "receivers": receivers, "senders": senders, "link_weights": {},
            "disabled": [], **extra}


def position(rel):
    """Where the walker is ``rel`` s after the first frame; None outside the walk."""
    if not WALK[0][0] <= rel <= WALK[-1][0]:
        return None
    for (t0, x0, y0), (t1, x1, y1) in zip(WALK, WALK[1:]):
        if rel <= t1:
            f = (rel - t0) / (t1 - t0)
            return x0 + f * (x1 - x0), y0 + f * (y1 - y0)


def distance(p, a, b):
    """Distance from the point p to the segment a-b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - u * dx, py - ay - u * dy)


def payload(rel):
    """The frame ``rel`` s after the first. The logged map is cold; the logged track is on the walker."""
    where = position(rel)
    rows = []
    for label, (a, b) in links().items():
        raised = where is not None and distance(where, a, b) <= REACH_M
        rows.append({"id": label, "receiver": label.split("<-")[0], "sender": None, "mac": "",
                     "state": "MOTION" if raised else "EMPTY", "mode": "fixed", "elevated": raised, "level": None,
                     "score": RISE if raised else 1.0, "threshold": 2.0, "normalised": None, "pkt_rate": 50.0,
                     "drops": 0, "rssi": -50, "age_s": 0.02, "baseline": None, "evidence": 0.0, "weight": 1.0,
                     "contributes": False})
    track = None
    if where is not None:
        track = {"active": True, "x": where[0], "y": where[1], "sx": 0.2, "sy": 0.2, "rho": 0.0, "vx": 0.0,
                 "vy": 0.0, "age_s": 1.0, "last_seen_s": 0.0, "trail": []}
    cells = [0.0] * (28 * 18)
    return {"room": {"width_m": W, "depth_m": D}, "nodes": [], "links": rows,
            "heatmap": {"cols": 28, "rows": 18, "extents": {"x_min": 0.0, "y_min": 0.0, "x_max": W, "y_max": D},
                        "cells": cells, "max": 0.0, "contributing": [], "busy_nodes": [], "estimator": "backprojection"},
            "status": {"time_s": T0 + rel, "room": {"state": "EMPTY", "elevated": [], "mode": "fixed"}},
            "track": track}


def write_run(tmp_path, room=None, heatmap=None, name="walk"):
    """A synthetic run: the log, its walks and its labels. Returns the three paths."""
    header = {"format": "csi-dashboard-log", "version": 1, "room": room_header() if room is None else room,
              "thresholds": {}, "mode": "fixed", "frame_interval_s": STEP,
              "heatmap": {"estimator": "auto"} if heatmap is None else heatmap, "notes": [], "source": "synthetic",
              "started": T0}
    frames = [payload(i * STEP) for i in range(int(END_S / STEP))]
    log, walks, labels = tmp_path / f"{name}.jsonl", tmp_path / f"{name}.csv", tmp_path / f"{name}_labels.csv"
    log.write_text("\n".join(json.dumps(x) for x in [header, *frames]) + "\n", encoding="utf-8")
    walks.write_text(WALK_CSV, encoding="utf-8")
    labels.write_text(LABELS_CSV, encoding="utf-8")
    return log, walks, labels


def load(tmp_path, **kwargs):
    log, walks, labels = write_run(tmp_path, **kwargs)
    return tune_replay.load_run({"log": log, "path": walks, "labels": labels})


# ---------------------------------------------------------------------------------------------- scoring

def test_a_miss_costs_the_cap_and_false_activity_adds_to_the_cost():
    m = tune_replay.metrics([0.5, 1.0, None, 2.0, 4.0], tracked=3, empty_frames=10, empty_false=1, cap=3.0,
                            empty_weight=1.0)
    assert m["frames"] == 5
    assert m["position_cost_m"] == pytest.approx((0.5 + 1.0 + 3.0 + 2.0 + 3.0) / 5)   # 4.0 capped, the miss at 3
    assert m["cost_m"] == pytest.approx(1.9 + 3.0 * 0.1)
    assert m["median_m"] == pytest.approx(1.5)            # of the frames with an estimate
    assert m["p90_m"] == pytest.approx(3.4)
    assert m["within_1.5"] == pytest.approx(0.4)          # of all the walk frames: the miss is not within
    assert m["within_3"] == pytest.approx(0.6)
    assert m["estimated"] == pytest.approx(0.8)
    assert m["track"] == pytest.approx(0.6)
    assert m["false_activity"] == pytest.approx(0.1)


def test_the_ranking_puts_the_estimate_nearest_the_truth_first():
    near = tune_replay.metrics([0.2] * 10)
    far = tune_replay.metrics([1.0] * 10)
    noisy = tune_replay.metrics([0.2] * 10, empty_frames=10, empty_false=5)   # near, but busy in the empty room
    blind = tune_replay.metrics([None] * 10)
    assert sorted([blind, noisy, far, near], key=tune_replay.rank_key) == [near, far, noisy, blind]


def test_the_best_is_the_simplest_setting_within_a_centimetre_of_the_lowest_cost():
    def entry(cost, heat, tracker=None):
        return {"heatmap": {"estimator": "backprojection", **heat}, "tracker": tracker or {}, "cost_m": cost,
                "median_m": cost, "within_1.5": 1.0}

    plain = entry(0.505, {})
    tweaked = entry(0.500, {"falloff_m": 0.5, "sensitivity": 2.0})     # 5 mm better: noise
    assert tune_replay.choose_best([tweaked, plain]) is plain
    clearly = entry(0.480, {"falloff_m": 0.5}, {"accel_sigma": 2.0})   # 2 cm better
    assert tune_replay.choose_best([tweaked, plain, clearly]) is clearly


def test_the_best_settings_become_the_flag_the_live_parameters_and_the_code_defaults():
    rec = tune_replay.recommend({"estimator": "rti", "evidence": "robust_z", "z_floor": 1.75, "z_full": 4.5,
                                 "rti_alpha": 0.07, "decay_tau_s": 1.1}, {"activity_min": 0.21, "keep_min": 0.13})
    assert rec["flags"] == ["--estimator", "rti"]
    assert rec["live"]["decay"] == 1.1 and rec["live_changed"]
    assert "falloff" not in rec["live"]                   # the footprint width is the back-projection's
    assert {(path, name): value for path, name, value in rec["code"]} == {
        ("csi/heatmap.py", "Z_FLOOR"): 1.75, ("csi/heatmap.py", "Z_FULL"): 4.5, ("csi/rti.py", "ALPHA"): 0.07,
        ("csi/heatmap.py", "DECAY_TAU_S"): 1.1, ("csi/tracker.py", "ACTIVITY_MIN"): 0.21,
        ("csi/tracker.py", "KEEP_MIN"): 0.13}
    assert rec["heatmap"]["rti_excess_m"] == tune_replay.HEATMAP_DEFAULTS["rti_excess_m"]   # written out in full


# ---------------------------------------------------------------------------------------------- inputs

def test_each_path_and_labels_file_belongs_to_the_log_before_it():
    args = tune_replay.build_parser().parse_args(["--log", "a.jsonl", "--path", "a.csv", "--log", "b.jsonl",
                                                  "--labels", "b.csv", "--path", "b_walk.csv"])
    assert args.runs == [{"log": Path("a.jsonl"), "path": Path("a.csv"), "labels": None},
                         {"log": Path("b.jsonl"), "path": Path("b_walk.csv"), "labels": Path("b.csv")}]


@pytest.mark.parametrize("argv", [["--path", "a.csv", "--log", "a.jsonl"],
                                  ["--log", "a.jsonl", "--path", "a.csv", "--path", "b.csv"]])
def test_a_walk_file_without_its_own_log_is_refused(argv):
    with pytest.raises(SystemExit):
        tune_replay.build_parser().parse_args(argv)


def test_a_run_replays_in_the_room_its_header_recorded(tmp_path):
    run = load(tmp_path)
    room = run.replay.room
    assert sorted(f"{rx.name}<-{tx.mac[-2:]}" for rx, tx in room.links()) == sorted(links())
    assert (room.width_m, room.depth_m) == (W, D)
    assert len(run.replay.truths) == WALK_FRAMES
    assert len(run.replay.empty) == EMPTY_FRAMES          # the empty label only; the walking label is not empty
    assert run.centre == (W / 2, D / 2)


def test_a_header_without_a_room_needs_the_room_file(tmp_path):
    log, walks, _ = write_run(tmp_path, room={})
    with pytest.raises(tune_replay.EvalError, match="--room"):
        tune_replay.load_run({"log": log, "path": walks, "labels": None})


def test_the_room_centre_is_the_middle_of_the_footprint_when_there_is_one(tmp_path):
    log, walks, _ = write_run(tmp_path, room=room_header(footprint=[[0, 0], [2, 0], [2, 3], [0, 3]]))
    run = tune_replay.load_run({"log": log, "path": walks, "labels": None})
    assert run.centre == pytest.approx((1.0, 1.5))


def test_without_labels_the_quiet_start_can_count_as_empty(tmp_path):
    log, walks, _ = write_run(tmp_path)
    run = tune_replay.load_run({"log": log, "path": walks, "labels": None}, empty_before_walks=True)
    assert len(run.replay.empty) == 8                     # 15 s after the start to 3 s before the walk: 15 <= t < 17


# ---------------------------------------------------------------------------------------------- replays

def test_a_deaf_rule_never_places_the_walker_and_a_rule_that_hears_does(tmp_path):
    evaluator = tune_replay.Evaluator([load(tmp_path)])
    deaf, hearing = evaluator.evaluate([(DEAF, {}), (HEARING, {})])
    assert deaf["frames"] == hearing["frames"] == WALK_FRAMES
    assert deaf["estimated"] == 0.0
    assert deaf["cost_m"] == pytest.approx(tune_replay.DEFAULT_CAP_M)
    assert hearing["estimated"] > 0.9
    assert hearing["cost_m"] < deaf["cost_m"]
    assert hearing["empty_frames"] == EMPTY_FRAMES
    assert hearing["false_activity"] == 0.0 and deaf["false_activity"] == 0.0   # the quiet room stays cold


def test_the_search_moves_from_a_deaf_start_to_the_rule_that_hears_the_walker(tmp_path):
    evaluator = tune_replay.Evaluator([load(tmp_path)])
    dims = [("evidence", [{"evidence": "ratio", "r_floor": 4.0, "r_full": 8.0},
                          {"evidence": "ratio", "r_floor": 1.2, "r_full": 3.0}])]
    best = tune_replay.descend(evaluator, DEAF, dims, passes=2)
    assert (best["r_floor"], best["r_full"]) == (1.2, 3.0)
    assert evaluator.replays == 2                         # one replay per setting: the second pass is all cached


def test_the_runs_are_pooled(tmp_path):
    runs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        runs.append(load(tmp_path / name))
    evaluator = tune_replay.Evaluator(runs)
    entry = evaluator.evaluate([(HEARING, {})])[0]
    assert entry["frames"] == 2 * WALK_FRAMES and entry["empty_frames"] == 2 * EMPTY_FRAMES
    assert evaluator.replays == 2                         # one replay per run


def test_the_references_score_the_logged_track_and_the_room_centre(tmp_path):
    run = load(tmp_path)
    logged = tune_replay.logged_reference([run])
    assert logged["estimated"] == 1.0 and logged["median_m"] == pytest.approx(0.0, abs=1e-9)
    centre = tune_replay.centre_reference([run])
    assert centre["median_m"] >= 0.7 - 1e-9               # the walk keeps 0.7 m from the centre's y


def test_the_command_line_scores_the_start_and_writes_settings_ready_to_load(tmp_path, capsys):
    log, walks, labels = write_run(tmp_path, heatmap={"estimator": "backprojection"})
    start, out = tmp_path / "deaf.json", tmp_path / "best.json"
    start.write_text(json.dumps({"heatmap": DEAF, "tracker": {}}), encoding="utf-8")
    argv = ["--log", str(log), "--path", str(walks), "--labels", str(labels), "--estimators", "backprojection",
            "--start", str(start), "--passes", "0", "--jobs", "1", "--out", str(out)]
    assert tune_replay.main(argv) == 0
    text = capsys.readouterr().out
    assert "dashboard flag: --estimator backprojection" in text
    assert "(as configured)" in text and "as logged" in text and "room centre" in text
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["format"] == "csi-tuned-settings"
    assert saved["heatmap"]["estimator"] == "backprojection"
    assert saved["heatmap"]["r_floor"] == tune_replay.HEATMAP_DEFAULTS["r_floor"]   # the defaults hear the walker
    assert [entry["heatmap"].get("r_floor") for entry in saved["ranking"]] == [None, 4.0]   # defaults left out
    assert saved["score"]["false_activity"] == 0.0
    assert saved["references"]["as_logged"]["median_m"] == pytest.approx(0.0, abs=1e-9)
    assert tune_replay.read_settings(out) == (saved["heatmap"], saved["tracker"])


def test_settings_this_version_does_not_know_are_refused(tmp_path):
    path = tmp_path / "odd.json"
    path.write_text(json.dumps({"heatmap": {"estimator": "rti", "warp_factor": 9}, "tracker": {}}), encoding="utf-8")
    with pytest.raises(tune_replay.EvalError, match="warp_factor"):
        tune_replay.read_settings(path)


@pytest.mark.slow
def test_the_command_line_search_finds_settings_that_hear_the_walker(tmp_path, capsys):
    log, walks, labels = write_run(tmp_path, heatmap={"estimator": "backprojection"})
    start, out = tmp_path / "deaf.json", tmp_path / "best.json"
    start.write_text(json.dumps({"heatmap": DEAF, "tracker": {}}), encoding="utf-8")
    argv = ["--log", str(log), "--path", str(walks), "--labels", str(labels), "--estimators", "backprojection",
            "--start", str(start), "--quick", "--jobs", "1", "--out", str(out)]
    assert tune_replay.main(argv) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["heatmap"]["evidence"] == "ratio" and saved["heatmap"]["r_floor"] < RISE
    assert saved["score"]["estimated"] > 0.9
    assert "best: backprojection" in capsys.readouterr().out


@pytest.mark.slow
def test_worker_processes_score_the_same_as_one_process(tmp_path):
    run = load(tmp_path)
    pairs = [(DEAF, {}), (HEARING, {}), (HEARING, {"accel_sigma": 2.0})]
    one = tune_replay.Evaluator([run]).evaluate(pairs)
    parallel = tune_replay.Evaluator([run], jobs=2)
    try:
        two = parallel.evaluate(pairs)
    finally:
        parallel.close()
    assert two == one


@pytest.mark.slow
def test_replaying_a_live_run_with_its_own_settings_redraws_its_map(tmp_path):
    """The live dashboard also steps the heatmap between frames; the replay steps it at the frames only. The maps
    should still agree: the replayed centroid is close to the logged one on the frames where the logged map is hot."""
    from csi.room import Walk
    from csi.server import DashboardServer
    from tests.test_dashboard_server import ROOM, calibrated_thresholds, quiet_then_streams

    log = tmp_path / "live.jsonl"
    walk = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5)
    server = DashboardServer(ROOM, quiet_then_streams(walk, duration_s=4.0, seed=4), calibrated_thresholds(), port=0,
                             log=log)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
    finally:
        server.stop()
    header, frames, logged, _, _ = tune_replay.read_log_frames(log)
    room = tune_replay.room_from_header(header["room"])
    heat = tune_replay.Heatmap(room, **header["heatmap"])
    extents = {"x_min": 0.0, "y_min": 0.0, "x_max": room.width_m, "y_max": room.depth_m}
    size = {"width_m": room.width_m, "depth_m": room.depth_m}
    hot_frames, gaps = 0, []
    for (t, rows), (hot, centroid, _) in zip(frames, logged):
        heat.advance(t, rows)
        if hot is None or hot[2] <= 0.1:
            continue
        hot_frames += 1
        replayed = tune_replay.eval_log._centroid(tune_replay.heatmap_payload(heat, extents), size)
        if replayed is not None:
            gaps.append(math.dist(replayed, centroid))
    assert hot_frames >= 5
    assert len(gaps) >= 0.8 * hot_frames
    assert statistics.median(gaps) < 0.25
