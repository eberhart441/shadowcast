#!/usr/bin/env python
"""Offline accuracy tuner: replay logged runs through the heatmap and the tracker with other settings, and rank the
settings by position error along labelled walks and false activity during labelled empty periods.

    tune_replay.py --log walk.jsonl --path walk.csv --absolute                        # tune on one run
    tune_replay.py --log walk.jsonl --path walk.csv --labels labels.csv --absolute    # empty periods scored too
    tune_replay.py --log a.jsonl --path a.csv --log b.jsonl --path b.csv --absolute --out best.json
    tune_replay.py --log c.jsonl --path c.csv --absolute --start best.json --passes 0  # check settings on new walks

Each --log is a run written with scripts/dashboard.py --log. The --path (walks, the format of eval_log.py --path) and
--labels (the format of eval_log.py --labels; its empty rows are scored for false activity) that follow a --log belong
to it. Times are seconds from the log's first frame, or host times with --absolute, as in eval_log.py.

Replay: at each frame of a log, the logged link rows (each link's state, score and age: what csi/dashboard.py hands
csi/heatmap.py) step a fresh csi.heatmap.Heatmap with the candidate settings to the frame's host time, so the quiet
baselines, the near-node veto, the estimator and the smoothing all run again. Its image goes into the payload's
heatmap block, rounded as csi/dashboard.py rounds it, and a csi.tracker.Tracker steps on that block. The link states
are as logged: the thresholds are not tuned. The live dashboard also steps the heatmap on packets between frames, which
a log does not hold, so replaying the run's own settings gives a map close to the logged one but not identical: compare
the "(as configured)" row with the "as logged" reference.

Score, over the frames of every walk of every run: the estimate is the active track, else the heatmap's weighted
centroid (cells at or above half the hottest) when the hottest cell is above --min-heat, as eval_log.py --path scores
it; a frame with neither is a miss. Reported: the median and 90th-percentile error (frames with an estimate), the
shares of the walk frames within 1.5 m and 3 m and with an estimate or an active track, and the share of the empty
frames with false activity (an active track, or a hottest cell at or above --empty-heat). The cost that ranks the
settings, in metres: the mean error capped at --cap (a miss costs --cap), plus --empty-weight x --cap x the false
activity share.

Search: coordinate descent for each estimator (backprojection, rti), from the dashboard's defaults: each setting in
turn (evidence rule and its thresholds; footprint width and crossing weight, or the RTI ellipse width lambda and
regularisation alpha; rise and decay; sensitivity) is swept over a few values with the others held, and the best value
is kept if it lowers the cost by at least 1 cm (smaller gains are noise); --passes rounds. Then the tracker settings
(start and keep levels, process noise, gate, blob cut) are swept one at a time on the two best heatmap settings, and the
values that gain at least 1 cm are tried together. The best is, of the settings within 1 cm of the lowest cost, the one
with the fewest changes from the defaults. Two references: "as logged" (the run's own heatmap and track) and "room
centre" (always guessing the middle of the room; a useful setting beats it clearly).

Output: the ranked table and the best settings as the dashboard flag (--estimator), the live heatmap parameters (the
page's sliders, or POST /api/params) and the code defaults for the rest. --out writes them as JSON: its "heatmap" and
"tracker" blocks are csi.heatmap.Heatmap and csi.tracker.Tracker keyword arguments, and --start reads them back.
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import inspect
import json
import math
import multiprocessing
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))

import csi.heatmap as heatmap_module  # noqa: E402
import csi.rti as rti_module  # noqa: E402
import csi.tracker as tracker_module  # noqa: E402
from csi.dashboard import PAYLOAD_DECIMALS  # noqa: E402
from csi.heatmap import Heatmap  # noqa: E402
from csi.room import Node, Room, load_room  # noqa: E402
from csi.tracker import Tracker  # noqa: E402


def _load_eval_log():
    """scripts/eval_log.py as the module ``eval_log`` (the one already imported, if any): its readers and scoring."""
    module = sys.modules.get("eval_log")
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location("eval_log", SCRIPTS / "eval_log.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_log"] = module          # registered before it runs: its dataclasses look their module up
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("eval_log", None)
        raise
    return module


eval_log = _load_eval_log()
EvalError = eval_log.EvalError

ESTIMATORS = ("backprojection", "rti")
AUTO_EVIDENCE = {"backprojection": "ratio", "rti": "robust_z"}   # the rule Heatmap(evidence="auto") uses with each
WITHIN_M = (1.5, 3.0)            # shares of the walk frames whose estimate is within these distances of the walker
DEFAULT_CAP_M = 3.0              # errors are capped here in the cost, and a frame without an estimate costs this much
DEFAULT_EMPTY_WEIGHT = 1.0       # cost of the false-activity share, in units of the cap
DEFAULT_EMPTY_HEAT = tracker_module.ACTIVITY_MIN   # an empty-period map this hot would start a track: false activity
DEFAULT_PASSES = 2
MIN_GAIN_M = 0.01                # a change must lower the cost by this much to be kept; smaller gains are noise
DEFAULT_TOP = 15
DEFAULT_JOBS = max(1, min(4, os.cpu_count() or 1))
TOP_HEATMAPS = 2                 # heatmap settings the tracker is tuned on
QUIET_LEAD_S = 15.0              # --empty-before-walks: from this long after the log starts (the baselines are warm) ...
QUIET_GAP_S = 3.0                # ... to this long before the run's first walk
LIVE_PARAMS = {"sensitivity": "sensitivity", "rise_tau_s": "rise", "decay_tau_s": "decay",
               "falloff_m": "falloff"}   # Heatmap keyword -> POST /api/params key (csi/server.py PARAM_RANGES)
RTI_CONSTANTS = {"rti_excess_m": "EXCESS_M", "rti_alpha": "ALPHA", "rti_delta_m": "PRIOR_DELTA_M"}   # in csi/rti.py
SHORT_NAMES = {"rti_excess_m": "lambda", "rti_alpha": "alpha", "rti_delta_m": "delta", "falloff_m": "falloff",
               "rise_tau_s": "rise", "decay_tau_s": "decay", "sensitivity": "sens", "agreement": "agree",
               "activity_min": "start", "keep_min": "keep", "accel_sigma": "accel", "gate_chi2": "gate",
               "blob_fraction": "blob"}
SETTINGS_FORMAT = "csi-tuned-settings"
RANKING_KEPT = 50                # entries of the ranking written by --out


def _defaults(cls) -> dict:
    """The keyword arguments of ``cls`` that have a default, with it."""
    return {name: p.default for name, p in inspect.signature(cls).parameters.items()
            if p.default is not inspect.Parameter.empty}


HEATMAP_DEFAULTS = _defaults(Heatmap)
TRACKER_DEFAULTS = _defaults(Tracker)


# ---------------------------------------------------------------------------------------------- search space

def _dedupe(values: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for value in values:
        key = _key(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _ratio(floor: float, full: float) -> dict:
    """The ratio evidence rule with these thresholds (the robust-z thresholds back at their defaults)."""
    return {"evidence": "ratio", "r_floor": floor, "r_full": full,
            "z_floor": heatmap_module.Z_FLOOR, "z_full": heatmap_module.Z_FULL}


def _robust(floor: float, full: float) -> dict:
    """The robust-z evidence rule with these thresholds (the ratio thresholds back at their defaults)."""
    return {"evidence": "robust_z", "z_floor": floor, "z_full": full,
            "r_floor": heatmap_module.R_FLOOR, "r_full": heatmap_module.R_FULL}


def heatmap_dims(estimator: str, quick: bool = False) -> list[tuple[str, list[dict]]]:
    """The heatmap settings the search sweeps for ``estimator``, in order: (name, values), each value a dict of
    Heatmap keyword arguments, the dashboard's default first. A setting this version of csi.heatmap lacks is left out."""
    h = heatmap_module
    smoothing = ("rise/decay", [{"rise_tau_s": r, "decay_tau_s": d}
                                for r, d in ((h.RISE_TAU_S, h.DECAY_TAU_S), (0.15, 0.3), (0.3, 1.0), (0.6, 0.6))])
    sensitivity = ("sensitivity", [{"sensitivity": v} for v in (h.SENSITIVITY, 0.8, 2.0)])
    if estimator == "backprojection":
        dims = [("evidence", [_ratio(h.R_FLOOR, h.R_FULL), _ratio(1.1, 2.0), _ratio(1.4, 4.0),
                              _robust(h.Z_FLOOR, h.Z_FULL), _robust(1.5, 4.0)]),
                ("falloff", [{"falloff_m": v} for v in (h.FALLOFF_M, 0.25, 0.5, 0.7)]),
                ("agreement", [{"agreement": v} for v in (h.AGREEMENT, 0.0, 0.8)]),
                smoothing, sensitivity]
    elif estimator == "rti":
        dims = [("evidence", [_robust(h.Z_FLOOR, h.Z_FULL), _robust(1.5, 4.0), _robust(3.0, 8.0),
                              _ratio(h.R_FLOOR, h.R_FULL)]),
                ("lambda", [{"rti_excess_m": v} for v in (rti_module.EXCESS_M, 0.15, 0.6, 1.0)]),
                ("alpha", [{"rti_alpha": v} for v in (rti_module.ALPHA, 0.05, 0.5, 1.0)]),
                smoothing, sensitivity]
    else:
        raise ValueError(f"unknown estimator {estimator!r}; expected one of {', '.join(ESTIMATORS)}")
    out = []
    for name, values in dims:
        values = _dedupe(v for v in values if set(v) <= set(HEATMAP_DEFAULTS))
        if values:
            out.append((name, values[:3] if quick else values))
    return out


def tracker_dims(quick: bool = False) -> list[tuple[str, list[dict]]]:
    """The tracker settings the search sweeps, as heatmap_dims, with Tracker keyword arguments."""
    t = tracker_module
    dims = [("start/keep", [{"activity_min": a, "keep_min": k}
                            for a, k in ((t.ACTIVITY_MIN, t.KEEP_MIN), (0.2, 0.12), (0.35, 0.2))]),
            ("accel", [{"accel_sigma": v} for v in (t.ACCEL_SIGMA, 0.5, 2.0)]),
            ("gate", [{"gate_chi2": v} for v in (t.GATE_CHI2, 5.99, 13.82)]),
            ("blob", [{"blob_fraction": v} for v in (t.BLOB_FRACTION, 0.4, 0.7)])]
    out = []
    for name, values in dims:
        values = _dedupe(v for v in values if set(v) <= set(TRACKER_DEFAULTS))
        if values:
            out.append((name, values))
    return out[:2] if quick else out


def family_start(estimator: str) -> dict:
    """The dashboard's defaults of every heatmap setting the search moves for ``estimator``, with that estimator and
    the evidence rule "auto" picks for it written out."""
    start = {}
    for _, values in heatmap_dims(estimator):
        for key in values[0]:
            start[key] = HEATMAP_DEFAULTS[key]
    start["estimator"] = estimator
    if "evidence" in HEATMAP_DEFAULTS:
        start["evidence"] = AUTO_EVIDENCE[estimator]
    return start


def _key(params: Mapping) -> tuple:
    return tuple(sorted(params.items()))


def _same(a, b) -> bool:
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def canonical_heatmap(params: Mapping) -> dict:
    """``params`` without what Heatmap would take by default anyway (the estimator is always kept); an evidence rule
    that is the one "auto" picks for the estimator counts as the default."""
    estimator = params.get("estimator", HEATMAP_DEFAULTS.get("estimator", "auto"))
    out = {"estimator": estimator}
    for key, value in params.items():
        if key == "estimator":
            continue
        if key == "evidence" and value in ("auto", AUTO_EVIDENCE.get(estimator)):
            continue
        if key in HEATMAP_DEFAULTS and _same(value, HEATMAP_DEFAULTS[key]):
            continue
        out[key] = value
    return out


def canonical_tracker(params: Mapping) -> dict:
    """``params`` without what Tracker would take by default anyway."""
    return {key: value for key, value in params.items()
            if not (key in TRACKER_DEFAULTS and _same(value, TRACKER_DEFAULTS[key]))}


def explicit_heatmap(params: Mapping) -> dict:
    """Every setting the search moves for the estimator, written out (the defaults where ``params`` has none)."""
    estimator = params.get("estimator", HEATMAP_DEFAULTS.get("estimator", "auto"))
    base = family_start(estimator) if estimator in ESTIMATORS else {"estimator": estimator}
    return {**base, **params}


def explicit_tracker(params: Mapping) -> dict:
    """Every tracker setting the search moves, written out (the defaults where ``params`` has none)."""
    base = {key: TRACKER_DEFAULTS[key] for _, values in tracker_dims() for value in values for key in value}
    return {**base, **params}


# ---------------------------------------------------------------------------------------------- runs

@dataclass(frozen=True)
class ScoreOptions:
    """How a frame is scored (see the module doc)."""
    min_heat: float = eval_log.DEFAULT_MIN_HEAT
    centroid_cut: float = eval_log.CENTROID_CUT
    empty_heat: float = DEFAULT_EMPTY_HEAT


@dataclass
class ReplayRun:
    """What a replay needs from one logged run; this is what the worker processes receive."""
    room: Room
    frames: list            # (host time, link rows as Heatmap.advance reads them: id, state, score, age_s), time order
    truths: dict            # frame index -> (x, y) of the walker, for the frames of the walks
    empty: frozenset        # frame indices in the empty periods


@dataclass
class LoadedRun:
    """One run: its files, its replay inputs and what the dashboard logged in it."""
    log_path: Path
    walks_path: Optional[Path]
    labels_path: Optional[Path]
    header: dict
    replay: ReplayRun
    logged: list            # per frame: (hottest cell, centroid, track) of the logged payload, as eval_log.py reads them
    centre: tuple           # the middle of the room (the "room centre" reference)
    walks: int
    span_s: float
    empty_source: str
    notes: list = field(default_factory=list)


def _frame(path: Path, n: int, obj) -> tuple[float, tuple[dict, ...]]:
    status = obj.get("status") if isinstance(obj, dict) else None
    links = obj.get("links") if isinstance(obj, dict) else None
    t = status.get("time_s") if isinstance(status, dict) else None
    if not isinstance(links, list) or not eval_log._is_number(t):
        raise EvalError(f"{path} line {n} is not a state payload (needs links and status.time_s)")
    rows = []
    for row in links:
        label = row.get("id") if isinstance(row, dict) else None
        if not isinstance(label, str):
            raise EvalError(f"{path} line {n} has a link row without an id")
        state, score, age = row.get("state"), row.get("score"), row.get("age_s")
        rows.append({"id": label, "state": state if isinstance(state, str) else None,
                     "score": float(score) if eval_log._is_number(score) else None,
                     "age_s": float(age) if eval_log._is_number(age) else None})
    return float(t), tuple(rows)


def _logged(obj: dict, cut: float) -> tuple:
    """(hottest cell, weighted centroid, track) of a logged payload, read as eval_log.py reads them."""
    heatmap, size = obj.get("heatmap"), obj.get("room")
    holder = obj if "track" in obj or not isinstance(heatmap, dict) else heatmap
    return eval_log._hottest(heatmap, size), eval_log._centroid(heatmap, size, cut), eval_log._track(holder.get("track"))


def read_log_frames(path, centroid_cut: float = eval_log.CENTROID_CUT) -> tuple[dict, list, list, float, list[str]]:
    """A scripts/dashboard.py --log file as (header, frames in time order, what each frame logged, the first frame's
    host time (time 0 of relative walk and label times), notes). It is read line by line and only what the replay needs
    is kept. A corrupt last line (a run stopped mid-write) is skipped with a note; any other bad line raises EvalError."""
    path = Path(path)
    header: Optional[dict] = None
    frames: list = []
    logged: list = []
    notes: list[str] = []
    bad: Optional[tuple[int, str]] = None
    try:
        with open(path, encoding="utf-8") as handle:
            for n, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if bad is not None:
                    raise EvalError(bad[1])
                try:
                    obj = json.loads(line)
                except ValueError as e:
                    bad = (n, f"{path} line {n} is corrupt: not valid JSON ({e})")
                    continue
                if header is None:
                    if not isinstance(obj, dict) or obj.get("format") != eval_log.LOG_FORMAT:
                        raise EvalError(f"{path} line {n} is not a dashboard log header (expected format "
                                        f"{eval_log.LOG_FORMAT!r}); record a run with scripts/dashboard.py --log")
                    header = obj
                    continue
                frames.append(_frame(path, n, obj))
                logged.append(_logged(obj, centroid_cut))
    except OSError as e:
        raise EvalError(f"cannot read log {path}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise EvalError(f"cannot read log {path}: not UTF-8 text ({e})") from e
    if header is None:
        raise EvalError(bad[1] if bad else f"{path} is empty: not a dashboard log")
    if not frames:
        raise EvalError(f"{path} has a header but no state frames")
    if bad is not None:
        notes.append(f"{path} line {bad[0]} is incomplete (the run was probably stopped mid-write) and was ignored")
    t0 = frames[0][0]
    if any(b[0] < a[0] for a, b in zip(frames, frames[1:])):
        order = sorted(range(len(frames)), key=lambda i: frames[i][0])
        frames, logged = [frames[i] for i in order], [logged[i] for i in order]
        notes.append(f"{path}: frames out of time order were sorted by time")
    return header, frames, logged, t0, notes


def _frozen(value):
    """JSON lists as tuples, so a list-valued room field (a footprint polygon) fits the frozen Room."""
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value


def room_from_header(spec) -> Optional[Room]:
    """The csi.room.Room a log header recorded (csi/server.py writes dataclasses.asdict(room)); None when the header
    has no room with receivers and senders. Fields this version of Room does not know are left out."""
    if not isinstance(spec, dict) or not spec.get("receivers") or not spec.get("senders"):
        return None
    node_keys = {f.name for f in fields(Node)}
    room_keys = {f.name for f in fields(Room)} - {"receivers", "senders", "link_weights", "disabled"}

    def nodes(items) -> tuple:
        return tuple(Node(**{k: v for k, v in item.items() if k in node_keys}) for item in items)

    extra = {k: _frozen(v) for k, v in spec.items() if k in room_keys}
    return Room(receivers=nodes(spec["receivers"]), senders=nodes(spec["senders"]),
                link_weights={str(k): float(v) for k, v in (spec.get("link_weights") or {}).items()},
                disabled=tuple(spec.get("disabled") or ()), **extra)


def _footprint(config) -> Optional[list[tuple[float, float]]]:
    """The footprint polygon of a room (a header's room, or a room.json): its "footprint", top level or under "room",
    as at least three (x, y) points in metres; None without one."""
    if not isinstance(config, dict):
        return None
    points = config.get("footprint")
    if points is None and isinstance(config.get("room"), dict):
        points = config["room"].get("footprint")
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        return None
    out = []
    for point in points:
        if not (isinstance(point, (list, tuple)) and len(point) == 2 and all(eval_log._is_number(v) for v in point)):
            return None
        out.append((float(point[0]), float(point[1])))
    return out


def room_centre(width_m: float, depth_m: float, footprint: Optional[Sequence[tuple[float, float]]] = None) -> tuple:
    """The middle of the room: the area centroid of its footprint polygon when it has one, else of the rectangle."""
    if footprint:
        points = list(footprint)
        area = cx = cy = 0.0
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            cross = x0 * y1 - x1 * y0
            area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        if abs(area) > 1e-12:
            return cx / (3.0 * area), cy / (3.0 * area)
    return width_m / 2.0, depth_m / 2.0


def _span_message(log_path: Path, walks_path, times: Sequence[float], t0: float, absolute: bool) -> str:
    if absolute:
        return (f"none of the walks in {walks_path} overlaps {log_path}, which covers host times {times[0]:.3f} to "
                f"{times[-1]:.3f}; are the walk times seconds from the log's first frame? then leave out --absolute")
    return (f"none of the walks in {walks_path} overlaps {log_path}, which covers {times[0] - t0:.2f} to "
            f"{times[-1] - t0:.2f} s from its first frame; are the walk times host times? then pass --absolute")


def load_run(spec: Mapping, *, absolute: bool = False, room: Optional[Room] = None, room_config=None,
             empty_labels: Iterable[str] = eval_log.DEFAULT_EMPTY_LABELS, empty_before_walks: bool = False,
             options: ScoreOptions = ScoreOptions()) -> LoadedRun:
    """One run from ``spec`` ({"log": path, "path": walks or None, "labels": labels or None}). ``room`` (with its
    room.json as ``room_config``) replaces the room the log's header recorded."""
    log_path = Path(spec["log"])
    walks_path, labels_path = spec.get("path"), spec.get("labels")
    header, frames, logged, t0, notes = read_log_frames(log_path, options.centroid_cut)
    if room is None:
        try:
            room = room_from_header(header.get("room"))
        except (TypeError, ValueError, AttributeError) as e:
            raise EvalError(f"{log_path}: the room in its header cannot be read ({e}); pass --room room.json") from None
        if room is None:
            raise EvalError(f"{log_path}: its header has no room with receivers and senders; pass --room room.json")
        footprint = _footprint(header.get("room"))
    else:
        footprint = _footprint(room_config)
        if isinstance(header.get("room"), dict) and header["room"].get("receivers"):
            notes.append(f"{log_path}: replayed in the --room geometry, not the room its header recorded")
    walks = eval_log.read_path(walks_path, absolute=absolute) if walks_path else []
    labels = eval_log.read_labels(labels_path, absolute=absolute) if labels_path else []
    offset = 0.0 if absolute else t0
    times = [t for t, _ in frames]
    truths: dict[int, tuple[float, float]] = {}
    for walk in walks:
        i0, i1 = bisect.bisect_left(times, walk.start + offset), bisect.bisect_left(times, walk.end + offset)
        if i0 == i1:
            notes.append(f"{walks_path}: walk {walk.name} (line {walk.line}) has no frames in {log_path}: not scored")
        for i in range(i0, i1):
            truths[i] = walk.at(times[i] - offset)
    if walks and not truths:
        notes.append(_span_message(log_path, walks_path, times, t0, absolute))
    names = {name.strip().lower() for name in empty_labels if name.strip()}
    empty: set[int] = set()
    for label in labels:
        if label.label.strip().lower() in names:
            end = math.inf if label.end is None else label.end + offset
            empty.update(range(bisect.bisect_left(times, label.start + offset), bisect.bisect_left(times, end)))
    source = f"labels {labels_path}" if labels_path else "none"
    if empty_before_walks and not labels and walks:
        start, end = times[0] + QUIET_LEAD_S, min(w.start for w in walks) + offset - QUIET_GAP_S
        if end > start:
            empty.update(range(bisect.bisect_left(times, start), bisect.bisect_left(times, end)))
        source = "the quiet start before the first walk (--empty-before-walks)"
    overlap = empty & set(truths)
    if overlap:
        notes.append(f"{labels_path or log_path}: {len(overlap)} frame(s) of empty periods are inside walks; they are "
                     "scored as walk frames only")
        empty -= overlap
    if labels_path and not empty:
        notes.append(f"{labels_path} has no frames labelled {', '.join(sorted(names))} in {log_path}: no empty "
                     "period scored for this run")
    replay = ReplayRun(room, frames, truths, frozenset(empty))
    return LoadedRun(log_path, Path(walks_path) if walks_path else None, Path(labels_path) if labels_path else None,
                     header, replay, logged, room_centre(room.width_m, room.depth_m, footprint), len(walks),
                     times[-1] - times[0], source, notes)


# ---------------------------------------------------------------------------------------------- replay

def heatmap_payload(heat: Heatmap, extents: Mapping) -> dict:
    """The payload's heatmap block as csi/dashboard.py builds it (DashboardState._heatmap_payload), with the cells
    rounded as it rounds them: the image the tracker reads."""
    cells = [round(v, PAYLOAD_DECIMALS) for v in heat.values.tolist()]
    return {"cols": heat.cols, "rows": heat.rows, "extents": extents, "cells": cells,
            "max": max(cells) if cells else 0.0, "contributing": sorted(heat.contributing),
            "busy_nodes": list(heat.busy_nodes), "estimator": heat.estimator_used}


def replay_run(run: ReplayRun, heat_params: Mapping, trackers: Sequence[Mapping],
               options: ScoreOptions = ScoreOptions()) -> list[tuple]:
    """Step a Heatmap with ``heat_params``, and one Tracker per entry of ``trackers`` on its image, through the run's
    frames as the dashboard steps them at each frame, and score each tracker's estimate. Per tracker: (the error at
    each walk frame, None for a miss; walk frames with an active track; empty frames; empty frames with false
    activity; {estimator: walk frames it drew})."""
    room = run.room
    heat = Heatmap(room, **heat_params)
    followers = [Tracker(**params) for params in trackers]
    size = {"width_m": room.width_m, "depth_m": room.depth_m}
    extents = {"x_min": 0.0, "y_min": 0.0, "x_max": room.width_m, "y_max": room.depth_m}
    errors: list[list] = [[] for _ in followers]
    tracked = [0] * len(followers)
    false = [0] * len(followers)
    empty_frames = 0
    used: dict[str, int] = {}
    for i, (t, rows) in enumerate(run.frames):
        heat.advance(t, rows)
        payload = heatmap_payload(heat, extents)
        truth = run.truths.get(i)
        quiet = i in run.empty
        if truth is None and not quiet:
            for follower in followers:
                follower.update(t, payload)
            continue
        hot = eval_log._hottest(payload, size)
        warm = hot is not None and hot[2] > options.min_heat
        centroid = eval_log._centroid(payload, size, options.centroid_cut) if warm and truth is not None else None
        lit = hot is not None and hot[2] >= options.empty_heat
        if truth is not None:
            used[payload["estimator"]] = used.get(payload["estimator"], 0) + 1
        empty_frames += quiet
        for k, follower in enumerate(followers):
            mark = eval_log._track(follower.update(t, payload))
            active = mark is not None and mark[2]
            if truth is not None:
                estimate = (mark[0], mark[1]) if active else centroid
                errors[k].append(None if estimate is None else math.dist(estimate, truth))
                tracked[k] += active
            if quiet:
                false[k] += active or lit
    return [(errors[k], tracked[k], empty_frames, false[k], used) for k in range(len(followers))]


def metrics(errors: Sequence[Optional[float]], tracked: int = 0, empty_frames: int = 0, empty_false: int = 0, *,
            cap: float = DEFAULT_CAP_M, empty_weight: float = DEFAULT_EMPTY_WEIGHT) -> dict:
    """The scores of one setting from its errors at the walk frames (None: no estimate), the walk frames with an active
    track, and the empty frames and those with false activity (see the module doc). Shares are of all the walk frames.
    The cost is None without walk frames."""
    n = len(errors)
    present = sorted(e for e in errors if e is not None)
    position = (math.fsum(min(e, cap) for e in present) + cap * (n - len(present))) / n if n else None
    false = empty_false / empty_frames if empty_frames else None
    cost = None if position is None else position + (empty_weight * cap * false if false is not None else 0.0)
    out = {"frames": n, "cost_m": cost, "position_cost_m": position,
           "median_m": statistics.median(present) if present else None,
           "p90_m": eval_log._percentile(present, 0.9) if present else None}
    for d in WITHIN_M:
        out[f"within_{d:g}"] = sum(e <= d for e in present) / n if n else None
    out.update({"estimated": len(present) / n if n else None, "track": tracked / n if n else None,
                "empty_frames": empty_frames, "false_activity": false})
    return out


def rank_key(entry: Mapping) -> tuple:
    """Lower is better: the cost, then the larger share within the first WITHIN_M distance, then the median."""
    cost, median = entry.get("cost_m"), entry.get("median_m")
    return (math.inf if cost is None else round(cost, 9), -(entry.get(f"within_{WITHIN_M[0]:g}") or 0.0),
            math.inf if median is None else median)


def _cost(entry: Mapping) -> float:
    return math.inf if entry.get("cost_m") is None else entry["cost_m"]


def better(a: Mapping, b: Mapping) -> bool:
    """Whether ``a`` costs at least MIN_GAIN_M less than ``b``."""
    return _cost(a) < _cost(b) - MIN_GAIN_M


def changes(entry: Mapping) -> int:
    """How many settings of an entry differ from the dashboard's defaults (a choice of estimator does not count)."""
    return len([key for key in entry["heatmap"] if key != "estimator"]) + len(entry["tracker"])


def choose_best(entries: Iterable[Mapping]) -> dict:
    """The entry to recommend: of those within MIN_GAIN_M of the lowest cost, the one with the fewest changes from the
    defaults, then the best ranked."""
    entries = list(entries)
    low = min(_cost(entry) for entry in entries)
    return min((entry for entry in entries if _cost(entry) <= low + MIN_GAIN_M),
               key=lambda entry: (changes(entry), rank_key(entry)))


_WORKER: dict = {}


def _start_worker(runs: list, options: ScoreOptions) -> None:
    _WORKER["runs"], _WORKER["options"] = runs, options


def _worker_replay(task: tuple) -> list[tuple]:
    index, heat_params, trackers = task
    return replay_run(_WORKER["runs"][index], heat_params, trackers, _WORKER["options"])


class Evaluator:
    """Replays the runs with pairs of heatmap and tracker settings and keeps every pair's scores, pooled over the runs.

    One replay per heatmap setting and run steps every tracker setting asked for with it. ``jobs`` above 1 replays in
    that many worker processes."""

    def __init__(self, runs: Sequence[LoadedRun], options: ScoreOptions = ScoreOptions(), *, jobs: int = 1,
                 cap: float = DEFAULT_CAP_M, empty_weight: float = DEFAULT_EMPTY_WEIGHT) -> None:
        self.runs = list(runs)
        self.options, self.jobs, self.cap, self.empty_weight = options, max(1, int(jobs)), cap, empty_weight
        self.entries: dict[tuple, dict] = {}     # (heatmap key, tracker key) -> scores and settings
        self.replays = 0
        self._pool: Optional[ProcessPoolExecutor] = None

    def evaluate(self, pairs: Iterable[tuple[Mapping, Mapping]]) -> list[dict]:
        """The entries of these (heatmap settings, tracker settings) pairs, in order; replays what is not known yet."""
        pairs = [(canonical_heatmap(heat), canonical_tracker(track)) for heat, track in pairs]
        todo: dict[tuple, tuple[dict, dict]] = {}
        for heat, track in pairs:
            if (_key(heat), _key(track)) not in self.entries:
                todo.setdefault(_key(heat), (heat, {}))[1].setdefault(_key(track), track)
        batches = [(heat, list(trackers.values())) for heat, trackers in todo.values()]
        tasks = [(r, heat, trackers) for heat, trackers in batches for r in range(len(self.runs))]
        results = self._map(tasks)
        self.replays += len(tasks)
        for b, (heat, trackers) in enumerate(batches):
            per_run = results[b * len(self.runs):(b + 1) * len(self.runs)]
            for k, track in enumerate(trackers):
                self.entries[(_key(heat), _key(track))] = self._entry(heat, track, [outcome[k] for outcome in per_run])
        return [self.entries[(_key(heat), _key(track))] for heat, track in pairs]

    def _entry(self, heat: dict, track: dict, outcomes: Sequence[tuple]) -> dict:
        errors: list = []
        tracked = empty = false = 0
        used: dict[str, int] = {}
        for run_errors, run_tracked, run_empty, run_false, run_used in outcomes:
            errors.extend(run_errors)
            tracked, empty, false = tracked + run_tracked, empty + run_empty, false + run_false
            for name, count in run_used.items():
                used[name] = used.get(name, 0) + count
        return {"heatmap": dict(heat), "tracker": dict(track), "estimators": used,
                **metrics(errors, tracked, empty, false, cap=self.cap, empty_weight=self.empty_weight)}

    def _map(self, tasks: list[tuple]) -> list[list[tuple]]:
        if self.jobs == 1 or len(tasks) < 2:
            return [replay_run(self.runs[r].replay, heat, trackers, self.options) for r, heat, trackers in tasks]
        if self._pool is None:
            # spawn on every platform: the same behaviour as macOS, and no fork of a process with BLAS threads
            self._pool = ProcessPoolExecutor(max_workers=self.jobs, mp_context=multiprocessing.get_context("spawn"),
                                             initializer=_start_worker,
                                             initargs=([run.replay for run in self.runs], self.options))
        return list(self._pool.map(_worker_replay, tasks))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def best(self) -> dict:
        return choose_best(self.entries.values())


# ---------------------------------------------------------------------------------------------- search

Progress = Optional[Callable[[str], None]]


def _argmin(entries: Sequence[Mapping]) -> int:
    return min(range(len(entries)), key=lambda i: rank_key(entries[i]))


def descend(evaluator: Evaluator, start: Mapping, dims: Sequence[tuple[str, list[dict]]],
            tracker: Optional[Mapping] = None, passes: int = DEFAULT_PASSES, progress: Progress = None,
            label: str = "") -> dict:
    """Coordinate descent over heatmap settings from ``start``: each setting in turn is swept over its values with the
    others held, and a value that costs at least MIN_GAIN_M less than the current settings is kept; at most ``passes``
    rounds, fewer when a round changes nothing. Returns the best heatmap settings found."""
    tracker = dict(tracker or {})
    current = dict(start)
    best = evaluator.evaluate([(current, tracker)])[0]
    for _ in range(passes):
        moved = False
        for name, values in dims:
            candidates = [{**current, **value} for value in values]
            entries = evaluator.evaluate([(candidate, tracker) for candidate in candidates])
            k = _argmin(entries)
            if better(entries[k], best):
                current, best, moved = candidates[k], entries[k], True
            if progress:
                progress(f"{label} {name}: {describe(best['heatmap'], best['tracker'])}, cost {_m(best['cost_m'])} m")
        if not moved:
            break
    return current


def tune_tracker(evaluator: Evaluator, heats: Sequence[Mapping], dims: Sequence[tuple[str, list[dict]]],
                 start: Optional[Mapping] = None, progress: Progress = None) -> None:
    """Sweep each tracker setting from ``start`` with the others held, on each of ``heats``; then try together the
    value of each setting that costs at least MIN_GAIN_M less than ``start``."""
    start = dict(start or {})
    variants = [start] + [{**start, **value} for _, values in dims for value in values]
    evaluator.evaluate([(heat, variant) for heat in heats for variant in variants])
    for heat in heats:
        base = evaluator.evaluate([(heat, start)])[0]
        combined = dict(start)
        for _, values in dims:
            entries = evaluator.evaluate([(heat, {**start, **value}) for value in values])
            k = _argmin(entries)
            if better(entries[k], base):
                combined.update(values[k])
        entry = evaluator.evaluate([(heat, combined)])[0]
        if progress:
            progress(f"tracker on {describe(heat)}: best of each setting together: {describe({}, combined, False)}, "
                     f"cost {_m(entry['cost_m'])} m")


def search(evaluator: Evaluator, *, estimators: Sequence[str] = ESTIMATORS, passes: int = DEFAULT_PASSES,
           tracker: bool = True, quick: bool = False, start: Optional[tuple[Mapping, Mapping]] = None,
           configured: Sequence[Mapping] = (), progress: Progress = None) -> dict:
    """Score ``configured`` (the runs' own settings) and the start, descend over the heatmap settings of each
    estimator, then tune the tracker on the best heatmap settings (unless ``tracker`` is False or ``passes`` is 0).
    Returns the best entry; every entry tried stays in ``evaluator.entries``."""
    start_heat, start_tracker = (dict(start[0]), dict(start[1])) if start else ({}, {})
    if configured:
        evaluator.evaluate([(settings, start_tracker) for settings in configured])
    families = list(estimators)
    chosen = start_heat.get("estimator")
    if chosen in ESTIMATORS and chosen not in families:
        families.append(chosen)
    if start_heat and chosen not in ESTIMATORS:      # "auto": score it as given, and search from the defaults
        evaluator.evaluate([(start_heat, start_tracker)])
    for estimator in families:
        begin = family_start(estimator)
        if chosen == estimator:
            begin.update(start_heat)
        descend(evaluator, begin, heatmap_dims(estimator, quick), start_tracker, passes, progress, estimator)
    if tracker and passes > 0:
        base = _key(canonical_tracker(start_tracker))
        heats, seen = [], set()
        for entry in sorted((e for e in evaluator.entries.values() if _key(e["tracker"]) == base), key=rank_key):
            if _key(entry["heatmap"]) not in seen:
                seen.add(_key(entry["heatmap"]))
                heats.append(entry["heatmap"])
            if len(heats) >= TOP_HEATMAPS:
                break
        tune_tracker(evaluator, heats, tracker_dims(quick), start_tracker, progress)
    return evaluator.best()


# ---------------------------------------------------------------------------------------------- references

def logged_reference(runs: Sequence[LoadedRun], options: ScoreOptions = ScoreOptions(), *, cap: float = DEFAULT_CAP_M,
                     empty_weight: float = DEFAULT_EMPTY_WEIGHT) -> dict:
    """The scores of what the runs logged: their own track, else their own map's centroid (as eval_log.py --path)."""
    errors: list = []
    tracked = empty = false = 0
    for run in runs:
        for i, (hot, centroid, mark) in enumerate(run.logged):
            truth, quiet = run.replay.truths.get(i), i in run.replay.empty
            if truth is None and not quiet:
                continue
            active = mark is not None and mark[2]
            if truth is not None:
                warm = hot is not None and hot[2] > options.min_heat
                estimate = (mark[0], mark[1]) if active else centroid if warm else None
                errors.append(None if estimate is None else math.dist(estimate, truth))
                tracked += active
            if quiet:
                empty += 1
                false += active or (hot is not None and hot[2] >= options.empty_heat)
    return metrics(errors, tracked, empty, false, cap=cap, empty_weight=empty_weight)


def centre_reference(runs: Sequence[LoadedRun], *, cap: float = DEFAULT_CAP_M) -> dict:
    """The scores of always guessing the middle of the room (no false-activity term: it always shows someone)."""
    errors = [math.dist(run.centre, truth) for run in runs for truth in run.replay.truths.values()]
    return metrics(errors, cap=cap, empty_weight=0.0)


# ---------------------------------------------------------------------------------------------- recommendation

def _relevant(key: str, estimator: str, rule: Optional[str]) -> bool:
    """Whether a heatmap setting changes anything with this estimator and evidence rule (None: either rule)."""
    if key in ("r_floor", "r_full"):
        return rule in (None, "ratio")
    if key in ("z_floor", "z_full"):
        return rule in (None, "robust_z")
    if key in ("falloff_m", "agreement"):
        return estimator != "rti"
    if key.startswith("rti_"):
        return estimator != "backprojection"
    return True


def _constant(module, path: str, name: str, key: str, owner: str) -> tuple[str, str]:
    return (path, name) if hasattr(module, name) else (path, f"{owner}({key}=...) default")


def recommend(heat: Mapping, tracker: Mapping) -> dict:
    """How to run the dashboard with these settings: its flag, the live heatmap parameters (POST /api/params keys),
    and the code defaults to change for the rest, as (file, name, value); plus every setting written out."""
    heat, tracker = explicit_heatmap(heat), explicit_tracker(tracker)
    estimator = heat.get("estimator", "auto")
    rule = heat.get("evidence", "auto")
    rule = AUTO_EVIDENCE.get(estimator) if rule == "auto" else rule
    live, live_changed = {}, False
    for key, name in LIVE_PARAMS.items():
        if key in heat and _relevant(key, estimator, rule):
            live[name] = heat[key]
            live_changed = live_changed or not _same(heat[key], HEATMAP_DEFAULTS.get(key))
    code = []
    for key in sorted(heat):
        value = heat[key]
        if key == "estimator" or not _relevant(key, estimator, rule):
            continue
        if key == "evidence":
            if value not in ("auto", AUTO_EVIDENCE.get(estimator)):
                code.append(("csi/heatmap.py", "Heatmap(evidence=...) default", value))
            continue
        if key in HEATMAP_DEFAULTS and _same(value, HEATMAP_DEFAULTS[key]):
            continue
        if key in RTI_CONSTANTS:
            code.append((*_constant(rti_module, "csi/rti.py", RTI_CONSTANTS[key], key, "Heatmap"), value))
        else:
            code.append((*_constant(heatmap_module, "csi/heatmap.py", key.upper(), key, "Heatmap"), value))
    for key in sorted(tracker):
        if not (key in TRACKER_DEFAULTS and _same(tracker[key], TRACKER_DEFAULTS[key])):
            code.append((*_constant(tracker_module, "csi/tracker.py", key.upper(), key, "Tracker"), tracker[key]))
    return {"flags": ["--estimator", estimator], "live": live, "live_changed": live_changed, "code": code,
            "heatmap": heat, "tracker": tracker}


def read_settings(path) -> tuple[dict, dict]:
    """The "heatmap" and "tracker" settings of a file written by --out (or any JSON object with those blocks)."""
    path = Path(path)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise EvalError(f"cannot read settings {path}: {e.strerror or e}") from e
    except ValueError as e:
        raise EvalError(f"cannot read settings {path}: not JSON ({e})") from e
    heat = obj.get("heatmap", {}) if isinstance(obj, dict) else None
    track = obj.get("tracker", {}) if isinstance(obj, dict) else None
    if not isinstance(heat, dict) or not isinstance(track, dict):
        raise EvalError(f"{path}: needs a JSON object with \"heatmap\" and \"tracker\" objects (as --out writes)")
    for block, given, known in (("heatmap", heat, HEATMAP_DEFAULTS), ("tracker", track, TRACKER_DEFAULTS)):
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise EvalError(f"{path}: {', '.join(unknown)} not {block} settings of this version (known: "
                            f"{', '.join(sorted(known))})")
    return heat, track


def check_settings(settings: tuple[Mapping, Mapping], room: Room, where) -> None:
    """Build a Heatmap (with its RTI projection) and a Tracker from ``settings``, so bad values fail before the search."""
    heat, track = settings
    try:
        heatmap = Heatmap(room, **heat)
        if heatmap.estimator in ("rti", "auto"):
            heatmap.projector()
        Tracker(**track)
    except (TypeError, ValueError) as e:
        raise EvalError(f"{where}: unusable settings: {e}") from None


# ---------------------------------------------------------------------------------------------- report

def _g(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _m(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def _literal(value) -> str:
    return json.dumps(value) if isinstance(value, str) else _g(value)


def describe(heat: Mapping, tracker: Optional[Mapping] = None, estimator: bool = True) -> str:
    """A short name for settings: the estimator and every setting that differs from the dashboard's defaults."""
    heat = canonical_heatmap(heat) if estimator or heat else {}
    parts = [str(heat["estimator"])] if estimator else []
    for key, value in sorted(heat.items()):
        if key == "estimator":
            continue
        parts.append(str(value) if key == "evidence" else f"{SHORT_NAMES.get(key, key)}={_g(value)}")
    moved = canonical_tracker(tracker or {})
    if moved:
        text = " ".join(f"{SHORT_NAMES.get(key, key)}={_g(value)}" for key, value in sorted(moved.items()))
        parts.append(f"| tracker {text}" if parts else text)
    return " ".join(parts) or "tracker defaults"


def _row(rank: str, name: str, entry: Mapping, cost: bool = True) -> list[str]:
    return [rank, name, _m(entry["cost_m"]) if cost else "-", _m(entry["median_m"]), _m(entry["p90_m"]),
            *(_pct(entry[f"within_{d:g}"]) for d in WITHIN_M), _pct(entry["estimated"]), _pct(entry["track"]),
            _pct(entry["false_activity"])]


def format_report(result: Mapping) -> str:
    """The readable report of a tuning result (see main)."""
    s, out = result["settings"], []
    for n, run in enumerate(result["runs"], start=1):
        out.append(f"run {n}: {run['log']}: {run['frames']} frames over {run['span_s']:.1f} s, {run['links']} links, "
                   f"room {run['room']}")
        out.append(f"  walks: {run['path'] or '-'}: {run['walks']} walk(s), {run['walk_frames']} frames; empty: "
                   f"{run['empty_frames']} frames ({run['empty_source']})")
    out.append(f"search: {', '.join(s['estimators'])}; {s['passes']} pass(es); tracker "
               f"{'tuned' if s['tracker'] else 'not tuned'}; {len(result['ranking'])} settings from "
               f"{result['replays']} replays in {result['elapsed_s']:.1f} s ({s['jobs']} job(s))")
    out.append(f"cost: mean error capped at {s['cap_m']:g} m (a frame with no estimate costs {s['cap_m']:g} m) + "
               f"{s['empty_weight']:g} x {s['cap_m']:g} m x the false-activity share of the empty frames")
    out.append(f"estimate: the active track, else the map's weighted centroid when its hottest cell is above "
               f"{s['min_heat']:g} (as eval_log.py --path); false activity: an active track or a cell at or above "
               f"{s['empty_heat']:g}")
    out.append(f"row 1, the best: of the settings within {MIN_GAIN_M:g} m of the lowest cost, the one with the fewest "
               "changes from the defaults; the others by cost")
    configured = {_key(c) for c in result["configured"]}
    rows = []
    for rank, entry in enumerate(result["ranking"][:s["top"]], start=1):
        name = describe(entry["heatmap"], entry["tracker"])
        if _key(entry["heatmap"]) in configured and entry["tracker"] == result.get("base_tracker", {}):
            name += " (as configured)"
        rows.append(_row(str(rank), name, entry))
    refs = result["references"]
    rows.append(_row("ref", "as logged (the run's own map and track)", refs["as_logged"]))
    rows.append(_row("ref", "room centre (always the middle of the room)", refs["room_centre"], cost=False))
    out += [""] + eval_log._table(["#", "settings", "cost m", "median m", "p90 m",
                                   *(f"<={d:g} m" for d in WITHIN_M), "estimate", "track", "empty false"], rows)
    if len(result["ranking"]) > s["top"]:
        out.append(f"  ... {len(result['ranking']) - s['top']} more (--top N shows more; --out writes them)")
    best, rec = result["best"], result["recommendation"]
    out += ["", f"best: {describe(best['heatmap'], best['tracker'])}: cost {_m(best['cost_m'])} m, median "
                f"{_m(best['median_m'])} m, p90 {_m(best['p90_m'])} m, "
                f"{_pct(best[f'within_{WITHIN_M[0]:g}'])} within {WITHIN_M[0]:g} m"]
    centre = refs["room_centre"]
    if best["median_m"] is not None and centre["median_m"] is not None and best["median_m"] >= 0.8 * centre["median_m"]:
        out.append(f"  warning: its median error is not clearly below the room centre's ({_m(centre['median_m'])} m): "
                   "the walks do not show that these settings localise")
    out.append(f"  dashboard flag: {' '.join(rec['flags'])}")
    if rec["live_changed"]:
        body = json.dumps(rec["live"])
        out.append("  live heatmap settings, until the dashboard restarts (the page's sliders, or):")
        out.append(f"    curl -s -X POST -H 'Content-Type: application/json' -d '{body}' "
                   "http://127.0.0.1:8000/api/params")
    else:
        out.append("  live heatmap settings (sensitivity, rise, decay, falloff): the defaults")
    if rec["code"]:
        out.append("  no flag yet: the code defaults to change (a default applies everywhere, tests included):")
        out += [f"    {path}: {name} = {_literal(value)}" for path, name, value in rec["code"]]
    else:
        out.append("  every other setting: the defaults")
    if result.get("out"):
        out.append(f"  all settings: {result['out']} (\"heatmap\": csi.heatmap.Heatmap keyword arguments, \"tracker\": "
                   "csi.tracker.Tracker keyword arguments; --start reads it)")
    logged, replayed = refs["as_logged"], result.get("configured_entry")
    if replayed and logged["median_m"] is not None and replayed["median_m"] is not None:
        out.append(f"  replay check: the run's own settings replayed offline have median {_m(replayed['median_m'])} m, "
                   f"the logged run {_m(logged['median_m'])} m")
    if result["notes"]:
        out += ["", "notes:"] + [f"  - {note}" for note in result["notes"]]
    return "\n".join(out) + "\n"


def _public(entry: Mapping) -> dict:
    """An entry for the JSON file: its name, its settings (Heatmap and Tracker keyword arguments that differ from the
    defaults) and its scores."""
    return {"settings": describe(entry["heatmap"], entry["tracker"]), **entry}


def build_result(runs: Sequence[LoadedRun], evaluator: Evaluator, *, settings: Mapping, configured: Sequence[Mapping],
                 elapsed_s: float, out: Optional[Path] = None, tracker: Optional[Mapping] = None) -> dict:
    """Everything format_report prints and --out writes. The ranking starts with the recommended entry (choose_best),
    then the others by cost. ``tracker``: the tracker settings the heatmap settings were searched with (--start's)."""
    best = evaluator.best()
    ranking = [best] + [entry for entry in sorted(evaluator.entries.values(), key=rank_key) if entry is not best]
    base_tracker = canonical_tracker(tracker or {})
    configured_entry = None
    if configured:
        configured_entry = evaluator.entries.get((_key(canonical_heatmap(configured[0])), _key(base_tracker)))
    notes = [note for run in runs for note in run.notes]
    if not any(run.replay.empty for run in runs):
        notes.append("no empty period was scored (give --labels with empty rows, or --empty-before-walks): the ranking "
                     "does not see false activity")
    if len(configured) > 1:
        notes.append(f"the runs were logged with {len(configured)} different settings; the replay check uses the first")
    summaries = []
    for run in runs:
        room = run.replay.room
        summaries.append({"log": str(run.log_path), "path": str(run.walks_path) if run.walks_path else None,
                          "labels": str(run.labels_path) if run.labels_path else None,
                          "frames": len(run.replay.frames), "span_s": run.span_s, "links": len(room.links()),
                          "room": f"{room.width_m:g} x {room.depth_m:g} m", "walks": run.walks,
                          "walk_frames": len(run.replay.truths), "empty_frames": len(run.replay.empty),
                          "empty_source": run.empty_source})
    references = {"as_logged": logged_reference(runs, evaluator.options, cap=evaluator.cap,
                                                empty_weight=evaluator.empty_weight),
                  "room_centre": centre_reference(runs, cap=evaluator.cap)}
    return {"runs": summaries, "settings": dict(settings), "ranking": ranking, "best": best,
            "recommendation": recommend(best["heatmap"], best["tracker"]), "references": references,
            "configured": [canonical_heatmap(c) for c in configured], "configured_entry": configured_entry,
            "base_tracker": base_tracker, "replays": evaluator.replays, "elapsed_s": elapsed_s,
            "out": str(out) if out else None, "notes": notes}


def result_json(result: Mapping) -> dict:
    """The --out file: the best settings, ready to load, then the scores behind them."""
    rec = result["recommendation"]
    return {"format": SETTINGS_FORMAT, "version": 1,
            "heatmap": rec["heatmap"], "tracker": rec["tracker"],
            "dashboard_flags": rec["flags"], "live_params": rec["live"],
            "code": [{"file": path, "name": name, "value": value} for path, name, value in rec["code"]],
            "score": _public(result["best"]),
            "references": result["references"],
            "ranking": [_public(entry) for entry in result["ranking"][:RANKING_KEPT]],
            "runs": result["runs"], "search": result["settings"], "replays": result["replays"],
            "notes": result["notes"]}


# ---------------------------------------------------------------------------------------------- command line

class _RunFile(argparse.Action):
    """--log starts a run; --path and --labels attach a file to the run of the --log before them."""

    def __call__(self, parser, namespace, values, option_string=None):
        runs = getattr(namespace, "runs", None)
        if runs is None:
            runs = []
            namespace.runs = runs
        if self.dest == "log":
            runs.append({"log": values, "path": None, "labels": None})
        elif not runs:
            parser.error(f"{option_string} {values} comes before any --log: give --log run.jsonl first, then its "
                         f"{option_string}")
        elif runs[-1][self.dest] is not None:
            parser.error(f"--log {runs[-1]['log']} has two {option_string} files; give each run its own --log")
        else:
            runs[-1][self.dest] = values


def _estimators(text: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
    if not names or any(name not in ESTIMATORS for name in names):
        raise argparse.ArgumentTypeError(f"estimators are {' and '.join(ESTIMATORS)}, comma separated; got {text!r}")
    return names


def _number(low: float, high: float = math.inf, *, low_open: bool = False, whole: bool = False):
    def parse(text: str):
        try:
            value = int(text) if whole else float(text)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and (value > low if low_open else value >= low) and value <= high):
            bound = f"above {low:g}" if low_open else f"at least {low:g}"
            limit = "" if high == math.inf else f" and at most {high:g}"
            raise argparse.ArgumentTypeError(f"must be a {'whole ' if whole else ''}number {bound}{limit}, got {text!r}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.set_defaults(runs=None)
    g = ap.add_argument_group("runs (repeat --log for each run; a --path or --labels belongs to the --log before it)")
    g.add_argument("--log", type=Path, action=_RunFile, metavar="run.jsonl",
                   help="a run written with scripts/dashboard.py --log")
    g.add_argument("--path", type=Path, action=_RunFile, metavar="walk.csv",
                   help="its walks (eval_log.py --path format): the position error is scored along them")
    g.add_argument("--labels", type=Path, action=_RunFile, metavar="labels.csv",
                   help="its labels (eval_log.py --labels format): the empty rows are scored for false activity")
    g.add_argument("--absolute", action="store_true",
                   help="walk and label times are host times (Unix seconds or ISO 8601), not seconds from the log's "
                        "first frame")
    g.add_argument("--room", type=Path, metavar="room.json",
                   help="room geometry for every log, instead of the room its header recorded")
    s = ap.add_argument_group("search")
    s.add_argument("--estimators", type=_estimators, default=ESTIMATORS, metavar="A,B",
                   help=f"estimators to tune (default {','.join(ESTIMATORS)})")
    s.add_argument("--passes", type=_number(0, whole=True), default=None, metavar="N",
                   help=f"coordinate-descent rounds over the heatmap settings (default {DEFAULT_PASSES}, 1 with "
                        "--quick); 0 scores the start settings only")
    s.add_argument("--quick", action="store_true",
                   help="one pass, three values per heatmap setting, and only the tracker's start/keep levels and "
                        "process noise")
    s.add_argument("--no-tracker", action="store_true", help="keep the tracker settings (its defaults, or --start's)")
    s.add_argument("--start", type=Path, metavar="best.json",
                   help="start from the settings in a file written by --out; with --passes 0, score just them")
    s.add_argument("--jobs", type=_number(1, whole=True), default=DEFAULT_JOBS, metavar="N",
                   help=f"worker processes (default {DEFAULT_JOBS})")
    c = ap.add_argument_group("scoring")
    c.add_argument("--cap", type=_number(0, low_open=True), default=DEFAULT_CAP_M, metavar="M",
                   help=f"errors are capped at M metres in the cost, and a frame with no estimate costs M "
                        f"(default {DEFAULT_CAP_M:g})")
    c.add_argument("--empty-weight", type=_number(0), default=DEFAULT_EMPTY_WEIGHT, metavar="W",
                   help=f"cost of false activity: W x the cap x its share of the empty frames "
                        f"(default {DEFAULT_EMPTY_WEIGHT:g})")
    c.add_argument("--empty-heat", type=_number(0, 1, low_open=True), default=DEFAULT_EMPTY_HEAT, metavar="V",
                   help=f"an empty frame whose hottest cell is at least V shows false activity (default "
                        f"{DEFAULT_EMPTY_HEAT:g}, the tracker's start level)")
    c.add_argument("--empty-labels", type=eval_log._names, default=eval_log.DEFAULT_EMPTY_LABELS, metavar="A,B",
                   help=f"labels that mark empty periods (default {','.join(eval_log.DEFAULT_EMPTY_LABELS)})")
    c.add_argument("--empty-before-walks", action="store_true",
                   help=f"for a run without --labels, score its frames from {QUIET_LEAD_S:g} s after the log starts "
                        f"to {QUIET_GAP_S:g} s before its first walk as empty (the walk protocol's quiet minute)")
    c.add_argument("--min-heat", type=eval_log._heat, default=eval_log.DEFAULT_MIN_HEAT, metavar="V",
                   help=f"a map whose hottest cell is at or below V gives no centroid estimate (default "
                        f"{eval_log.DEFAULT_MIN_HEAT:g}, as eval_log.py)")
    o = ap.add_argument_group("output")
    o.add_argument("--top", type=_number(1, whole=True), default=DEFAULT_TOP, metavar="N",
                   help=f"rows of the ranked table (default {DEFAULT_TOP})")
    o.add_argument("--out", type=Path, metavar="best.json",
                   help="write the best settings (Heatmap and Tracker keyword arguments) and the ranking as JSON")
    return ap


def _progress(started: float) -> Callable[[str], None]:
    def show(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f} s] {message}", file=sys.stderr, flush=True)
    return show


def _room_option(path: Optional[Path]) -> tuple[Optional[Room], Optional[dict]]:
    if path is None:
        return None, None
    try:
        return load_room(path), json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise EvalError(f"cannot read room {path}: {e}") from None


def configured_settings(runs: Sequence[LoadedRun]) -> list[dict]:
    """The distinct heatmap settings the runs were logged with (their header's "heatmap" keyword arguments)."""
    out, seen = [], set()
    for run in runs:
        given = run.header.get("heatmap")
        settings = canonical_heatmap({k: v for k, v in given.items() if k in HEATMAP_DEFAULTS}
                                     if isinstance(given, dict) else {})
        if _key(settings) not in seen:
            seen.add(_key(settings))
            out.append(settings)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    specs = args.runs or []
    if not specs:
        parser.error("give at least one --log run.jsonl, each followed by its --path walk.csv (and --labels)")
    for spec in specs:
        if spec["path"] is None and spec["labels"] is None:
            parser.error(f"--log {spec['log']} needs a --path or --labels after it")
    if not any(spec["path"] for spec in specs):
        parser.error("give at least one --path: the position error is scored along the walks")
    passes = args.passes if args.passes is not None else 1 if args.quick else DEFAULT_PASSES
    options = ScoreOptions(min_heat=args.min_heat, empty_heat=args.empty_heat)
    started = time.monotonic()
    progress = _progress(started)
    evaluator = None
    try:
        room, room_config = _room_option(args.room)
        runs = [load_run(spec, absolute=args.absolute, room=room, room_config=room_config,
                         empty_labels=args.empty_labels, empty_before_walks=args.empty_before_walks, options=options)
                for spec in specs]
        if not any(run.replay.truths for run in runs):
            raise EvalError("no walk has frames in its log: " + "; ".join(n for run in runs for n in run.notes))
        start = read_settings(args.start) if args.start is not None else None
        if start is not None:
            check_settings(start, runs[0].replay.room, args.start)
        configured = []
        for settings in configured_settings(runs):
            try:
                check_settings((settings, {}), runs[0].replay.room, "the logged heatmap settings")
                configured.append(settings)
            except EvalError as e:
                runs[0].notes.append(f"{e}; not replayed")
        evaluator = Evaluator(runs, options, jobs=args.jobs, cap=args.cap, empty_weight=args.empty_weight)
        progress(f"replaying {sum(len(run.replay.frames) for run in runs)} frames of {len(runs)} run(s) per setting, "
                 f"{args.jobs} job(s)")
        search(evaluator, estimators=args.estimators, passes=passes, tracker=not args.no_tracker, quick=args.quick,
               start=start, configured=configured, progress=progress)
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if evaluator is not None:
            evaluator.close()
    settings = {"estimators": list(args.estimators), "passes": passes, "tracker": not args.no_tracker and passes > 0,
                "quick": args.quick, "jobs": args.jobs, "cap_m": args.cap, "empty_weight": args.empty_weight,
                "empty_heat": args.empty_heat, "min_heat": args.min_heat, "top": args.top, "absolute": args.absolute}
    result = build_result(runs, evaluator, settings=settings, configured=configured,
                          elapsed_s=time.monotonic() - started, out=args.out, tracker=start[1] if start else None)
    if args.out is not None:
        try:
            args.out.write_text(json.dumps(result_json(result), indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {args.out}: {e.strerror or e}", file=sys.stderr)
            return 1
    print(format_report(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
