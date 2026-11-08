#!/usr/bin/env python
"""Score a labelled dashboard run: detection, latency, false alarms, heatmap localisation and a scaling curve.

    eval_log.py --log run.jsonl --labels labels.csv                  # readable summary on stdout
    eval_log.py --log run.jsonl --labels labels.csv --out run.json   # also the full result as JSON
    eval_log.py --log run.jsonl --labels labels.csv --absolute       # label times are host times
    eval_log.py --log night.jsonl --labels quiet.csv                 # quiet.csv: one row "0,,empty"

The log is a run recorded with scripts/dashboard.py --log run.jsonl: a header line, then one state payload per
frame. The labels are a CSV file with the columns start_s,end_s,label and an optional zone (the header row is
optional; without one the columns are in that order):

    start_s,end_s,label,zone
    0,60,empty,
    60,90,walking,"0.5,0.4,1.5,1.2"
    90,,empty,

Times are seconds from the first frame of the log. With --absolute they are host times instead: Unix seconds, as
in the payloads' status.time_s, or ISO 8601 date-times (local time unless they name a zone). An empty end_s runs
to the end of the log. A zone is a rectangle x0,y0,x1,y1 in metres, in room coordinates (origin at the top-left
corner, y down, as on the page), quoted, or unquoted when it is the last column. Labels in --motion-labels are
scored for detection, labels in --empty-labels for false alarms, and any other label (such as still_standing) is
reported with its share of frames in MOTION. Lines starting with # are comments.

Per link, and for the room state the dashboard showed:
    episodes detected      motion labels with at least one frame in MOTION
    frames in MOTION       share of the motion labels' frames in MOTION
    latency                label start to the first frame in MOTION, for each detected episode
    false alarms per hour  rising edges into MOTION during empty labels, per hour of decided time
    MOTION while empty     share of the empty labels' decided frames in MOTION
A frame in which a link (or the room) has no decision (WARMUP, UNCALIBRATED, NO_SIGNAL) counts as a miss in a
motion label and is left out of the false-alarm time, so neither rate flatters the system. Each link also gets its
median level (score / threshold, 1 at the threshold) during the motion and the empty labels, and a health summary
while it had signal: packets/s, and where the log has them the sender's rate and loss from its sequence counter,
clipping, noise floor and gains. A gain that changes during the run is flagged: a receiver reset re-rolls it.

Heatmap localisation, for motion labels with a zone: the share of their frames whose hottest cell has its centre
inside the zone. A frame whose map is cold (hottest cell at or below --min-heat) has no hottest cell and counts
as a miss; the share among the hot frames and the median distance from the hottest cell to the zone are given too.

Scaling curve: for each number of links n, room MOTION is recomputed as "at least k of the n links in MOTION"
(k capped at n) for every subset of n links, or for --max-subsets subsets drawn with a fixed seed when there are
more, and the rates are averaged over the subsets. It votes over the logged link states without the room's hold
time and strong-link rule, so its point for all the links can differ a little from the room row.

Intervals are Wilson 95% score intervals. A false-alarm rate's interval is the Wilson interval of alarms per
decided frame, scaled to an hour, so zero alarms in H hours has an upper bound of about 3.8 / H per hour. Frames
are not independent (the detectors smooth and debounce), so the intervals of frame shares are too narrow: quote
the episode counts. --out writes everything as JSON: per subject, per episode (the room and each link), the
localisation, every subset of the scaling curve, link health and notes. Standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import json
import math
import random
import re
import statistics
import sys
from array import array
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

LOG_FORMAT = "csi-dashboard-log"   # first line of a dashboard --log file (csi/server.py LOG_FORMAT)
MOTION = "MOTION"
NO_SIGNAL = "NO_SIGNAL"
DECIDED = frozenset({"EMPTY", MOTION, "STILL_PRESENT"})  # WARMUP, UNCALIBRATED and NO_SIGNAL are no decision
NO_DECISION, NO_MOTION, IN_MOTION = 0, 1, 2               # per-frame code of a link, the room, or a vote
TO_MOTION = bytes.maketrans(bytes([0, 1, 2]), bytes([0, 0, 1]))
TO_DECIDED = bytes.maketrans(bytes([0, 1, 2]), bytes([0, 1, 1]))
RISE = bytes([0, 1])                                      # a rising edge in a 0/1 motion sequence
ROOM = "room"
MOTION_KIND, EMPTY_KIND, OTHER_KIND = "motion", "empty", "other"
DEFAULT_MOTION_LABELS = ("walking", "motion")
DEFAULT_EMPTY_LABELS = ("empty",)
DEFAULT_K = (2,)             # the dashboard's room rule needs two links in MOTION (csi/adaptive.py ROOM_MIN_LINKS)
DEFAULT_MAX_SUBSETS = 50
DEFAULT_MIN_HEAT = 0.05
FALLBACK_FRAME_S = 0.2       # csi/server.py FRAME_INTERVAL_S, for a log too short to measure its own
MAX_GAP_FRAMES = 5           # a gap between frames longer than this many frame intervals is not observed time
HEALTH_KEYS = ("pkt_rate", "seq_rate", "seq_loss", "clip_pct", "rssi", "noise_floor")
GAIN_KEYS = ("agc_gain", "fft_gain")
HEALTH_SAMPLE_S = 1.0        # at most one health sample per link per second of host time
Z95 = 1.959963984540054      # two-sided 95% quantile of the normal distribution
LABEL_COLUMNS = ("start_s", "end_s", "label", "zone")


class EvalError(ValueError):
    """An input cannot be used; the message says what is wrong and where."""


# ---------------------------------------------------------------------------------------------- the log

@dataclass
class Log:
    """What the evaluation needs from a dashboard log: one entry per frame, in time order."""
    path: Path
    header: dict
    t0: float                         # host time of the first frame in the file: label time 0
    times: list[float]                # status.time_s per frame, ascending
    room: bytes                       # room state code per frame
    links: list[str]                  # link labels in order of first appearance: bit i of a mask is links[i]
    motion: list[int]                 # per frame: mask of the links in MOTION
    decided: list[int]                # per frame: mask of the links with a decision (EMPTY or MOTION)
    levels: dict[str, array]          # link -> level (score / threshold) per frame, NaN when absent
    hot: list[Optional[tuple[float, float, float]]]   # per frame: hottest cell (x m, y m, value), None without a map
    states: dict[str, Counter]        # "room" and each link -> frames per state over the whole log
    health: dict[str, dict[str, array]]  # link -> health key -> samples taken while the link had signal
    drops: dict[str, int]             # link -> its last cumulative drop count
    notes: list[str] = field(default_factory=list)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _code(state) -> int:
    if state == MOTION:
        return IN_MOTION
    return NO_MOTION if state in DECIDED else NO_DECISION


def _hottest(heatmap, room) -> Optional[tuple[float, float, float]]:
    """Centre (x, y) in metres and value of the hottest cell (the first in row-major order on a tie)."""
    if not isinstance(heatmap, dict):
        return None
    cols, rows, cells = heatmap.get("cols"), heatmap.get("rows"), heatmap.get("cells")
    if not (isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0
            and isinstance(cells, list) and len(cells) == cols * rows):
        return None
    ext = heatmap.get("extents") if isinstance(heatmap.get("extents"), dict) else {}
    room = room if isinstance(room, dict) else {}
    x_min, y_min = ext.get("x_min", 0.0), ext.get("y_min", 0.0)
    x_max, y_max = ext.get("x_max", room.get("width_m")), ext.get("y_max", room.get("depth_m"))
    if not all(_is_number(v) for v in (x_min, y_min, x_max, y_max)):
        return None
    try:
        value = max(cells)
        index = cells.index(value)
    except (TypeError, ValueError):
        return None
    if not _is_number(value):
        return None
    r, c = divmod(index, cols)
    return (x_min + (c + 0.5) * (x_max - x_min) / cols, y_min + (r + 0.5) * (y_max - y_min) / rows, float(value))


class _LogBuilder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.header: Optional[dict] = None
        self.times: list[float] = []
        self.room = bytearray()
        self.links: list[str] = []
        self.bits: dict[str, int] = {}
        self.motion: list[int] = []
        self.decided: list[int] = []
        self.levels: dict[str, array] = {}
        self.hot: list[Optional[tuple[float, float, float]]] = []
        self.states: dict[str, Counter] = {ROOM: Counter()}
        self.health: dict[str, dict[str, array]] = {}
        self.sampled_at: dict[str, float] = {}
        self.drops: dict[str, int] = {}

    def add(self, n: int, obj) -> None:
        if self.header is None:
            if not isinstance(obj, dict) or obj.get("format") != LOG_FORMAT:
                raise EvalError(f"{self.path} line {n} is not a dashboard log header (expected format "
                                f"{LOG_FORMAT!r}); record a run with scripts/dashboard.py --log run.jsonl")
            self.header = obj
            return
        status = obj.get("status") if isinstance(obj, dict) else None
        rows = obj.get("links") if isinstance(obj, dict) else None
        t = status.get("time_s") if isinstance(status, dict) else None
        if not isinstance(rows, list) or not _is_number(t):
            raise EvalError(f"{self.path} line {n} is not a state payload (needs links and status.time_s)")
        t = float(t)
        frame = len(self.times)
        self.times.append(t)
        decision = status.get("room")     # the room decision; obj["room"] holds the room's size
        room_state = decision.get("state") if isinstance(decision, dict) else None
        room_state = room_state if isinstance(room_state, str) else None
        self.room.append(_code(room_state))
        self.states[ROOM][room_state or "none"] += 1
        motion = decided = 0
        levels: dict[str, float] = {}
        for row in rows:
            label = row.get("id") if isinstance(row, dict) else None
            if not isinstance(label, str):
                raise EvalError(f"{self.path} line {n} has a link row without an id")
            bit = self.bits.get(label)
            if bit is None:
                bit = self.bits[label] = len(self.links)
                self.links.append(label)
                self.levels[label] = array("d", [math.nan]) * frame
                self.states[label] = Counter()
            state = row.get("state") if isinstance(row.get("state"), str) else None
            self.states[label][state or "none"] += 1
            if state in DECIDED:
                decided |= 1 << bit
                if state == MOTION:
                    motion |= 1 << bit
            level = row.get("level")
            levels[label] = float(level) if _is_number(level) else math.nan
            if state is not None and state != NO_SIGNAL:
                self._sample_health(label, t, row)
            if _is_number(row.get("drops")):
                self.drops[label] = int(row["drops"])
        for label, values in self.levels.items():
            values.append(levels.get(label, math.nan))
        self.motion.append(motion)
        self.decided.append(decided)
        self.hot.append(_hottest(obj.get("heatmap"), obj.get("room")))

    def _sample_health(self, label: str, t: float, row: dict) -> None:
        last = self.sampled_at.get(label)
        if last is not None and t - last < HEALTH_SAMPLE_S:
            return
        self.sampled_at[label] = t
        samples = self.health.setdefault(label, {})
        for key in HEALTH_KEYS + GAIN_KEYS:
            if _is_number(row.get(key)):
                samples.setdefault(key, array("d")).append(float(row[key]))

    def finish(self, bad: Optional[tuple[int, str]]) -> Log:
        if self.header is None:
            raise EvalError(bad[1] if bad else f"{self.path} is empty: not a dashboard log")
        if not self.times:
            raise EvalError(f"{self.path} has a header but no state frames")
        notes = []
        if bad is not None:
            notes.append(f"line {bad[0]} of the log is incomplete (the run was probably stopped mid-write) and was "
                         "ignored")
        count = len(self.times)
        for counter in self.states.values():
            missing = count - sum(counter.values())
            if missing:
                counter["absent"] += missing
        t0 = self.times[0]
        times, room, motion, decided, hot = self.times, bytes(self.room), self.motion, self.decided, self.hot
        levels = self.levels
        late = sum(b < a for a, b in zip(times, times[1:]))
        if late:
            order = sorted(range(count), key=times.__getitem__)
            times = [times[i] for i in order]
            room = bytes(room[i] for i in order)
            motion = [motion[i] for i in order]
            decided = [decided[i] for i in order]
            hot = [hot[i] for i in order]
            levels = {label: array("d", (values[i] for i in order)) for label, values in levels.items()}
            notes.append(f"{late} frame(s) were out of time order; the frames were sorted by time")
        return Log(self.path, self.header, t0, times, room, list(self.links), motion, decided, levels, hot,
                   self.states, self.health, self.drops, notes)


def read_log(path) -> Log:
    """Load a scripts/dashboard.py --log file. A corrupt last line (a run stopped mid-write) is skipped with a note;
    any other unreadable line raises EvalError naming it."""
    path = Path(path)
    builder = _LogBuilder(path)
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
                builder.add(n, obj)
    except OSError as e:
        raise EvalError(f"cannot read log {path}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise EvalError(f"cannot read log {path}: not UTF-8 text ({e})") from e
    return builder.finish(bad)


# ---------------------------------------------------------------------------------------------- labels

@dataclass(frozen=True)
class Label:
    start: float                      # seconds from the first frame, or host time with --absolute
    end: Optional[float]              # None: to the end of the log
    label: str
    zone: Optional[tuple[float, float, float, float]] = None   # x0, y0, x1, y1 with x0 < x1 and y0 < y1
    line: int = 0                     # line in the labels file


def parse_zone(text: str) -> tuple[float, float, float, float]:
    """``"x0,y0,x1,y1"`` (commas, semicolons or spaces) as a rectangle with its corners in order."""
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        values = []
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError(f"zone {text!r} is not four numbers x0,y0,x1,y1 in metres")
    x0, y0, x1, y1 = values
    if x0 == x1 or y0 == y1:
        raise ValueError(f"zone {text!r} has no area")
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _label_time(text: str, absolute: bool, where: str) -> float:
    try:
        value = float(text)
    except ValueError:
        if not absolute:
            raise EvalError(f"{where}: {text!r} is not a number of seconds (date-times need --absolute)") from None
        try:
            moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            raise EvalError(f"{where}: {text!r} is neither seconds nor an ISO 8601 date-time") from None
        value = moment.timestamp()   # a date-time without a zone is local time
    if not math.isfinite(value):
        raise EvalError(f"{where}: {text!r} is not a finite time")
    return value


def read_labels(path, absolute: bool = False) -> list[Label]:
    """Read a labels CSV (see the module doc). Raises EvalError naming the file and line of a bad row."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise EvalError(f"cannot read labels {path}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise EvalError(f"cannot read labels {path}: not UTF-8 text ({e})") from e
    columns: Optional[dict[str, int]] = None
    zone_last = True              # the zone is the last column, so an unquoted zone's commas can be joined back
    labels: list[Label] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [cell.strip() for cell in next(csv.reader([line], skipinitialspace=True))]
        names = [cell.lower() for cell in cells]
        if columns is None:
            columns = {name: i for i, name in enumerate(LABEL_COLUMNS)}
            if any(name in LABEL_COLUMNS for name in names):     # a header row
                missing = [name for name in LABEL_COLUMNS[:3] if name not in names]
                if missing:
                    raise EvalError(f"{path} line {n}: the header needs the columns start_s,end_s,label (and "
                                    f"optionally zone); {', '.join(missing)} missing")
                columns = {name: names.index(name) for name in LABEL_COLUMNS if name in names}
                zone_last = columns.get("zone") == len(names) - 1
                continue
        labels.append(_label_row(cells, columns, zone_last, absolute, f"{path} line {n}", n))
    if not labels:
        raise EvalError(f"{path} has no labels: write rows of start_s,end_s,label[,zone]")
    return labels


def _label_row(cells: list[str], columns: dict[str, int], zone_last: bool, absolute: bool, where: str,
               n: int) -> Label:
    def cell(name: str) -> str:
        i = columns.get(name)
        return cells[i] if i is not None and i < len(cells) else ""

    if not cell("start_s") or not cell("label"):
        raise EvalError(f"{where}: needs start_s, end_s and label, got {','.join(cells)!r}")
    start = _label_time(cell("start_s"), absolute, where)
    end = _label_time(cell("end_s"), absolute, where) if cell("end_s") else None
    if end is not None and not end > start:
        raise EvalError(f"{where}: end_s {cell('end_s')} is not after start_s {cell('start_s')}")
    zone_at = columns.get("zone")
    if zone_at is not None and zone_last:
        zone_text = ",".join(c for c in cells[zone_at:] if c)   # an unquoted x0,y0,x1,y1 spans four cells
    else:
        zone_text = cell("zone")
    try:
        zone = parse_zone(zone_text) if zone_text else None
    except ValueError as e:
        raise EvalError(f"{where}: {e}") from None
    return Label(start, end, cell("label"), zone, n)


# ---------------------------------------------------------------------------------------------- statistics

def wilson(k: float, n: float, z: float = Z95) -> Optional[tuple[float, float]]:
    """Wilson score interval of k successes in n trials (k may be a mean count), or None when n is 0."""
    if n <= 0:
        return None
    p = min(1.0, max(0.0, k / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denom
    low = 0.0 if k <= 0 else max(0.0, centre - half)
    high = 1.0 if k >= n else min(1.0, centre + half)
    return low, high


def _r(value: Optional[float]) -> Optional[float]:
    """A number for the JSON result: whole counts stay ints, other values are rounded to 6 decimals."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    return round(float(value), 6)


def proportion(k: float, n: float) -> dict:
    """``{count, total, rate, ci95}`` with the Wilson 95% interval; rate and ci95 are None when n is 0."""
    ci = wilson(k, n)
    return {"count": _r(k), "total": _r(n), "rate": _r(k / n) if n > 0 else None,
            "ci95": [_r(ci[0]), _r(ci[1])] if ci else None}


def per_hour(alarms: float, frames: float, seconds: float) -> dict:
    """Alarms per hour of observed time, with the Wilson 95% interval of alarms per frame scaled to an hour."""
    hours = seconds / 3600.0
    ci = wilson(alarms, frames)
    scale = frames / hours if hours > 0 else None
    return {"count": _r(alarms), "hours": _r(hours), "frames": _r(frames),
            "rate": _r(alarms / hours) if hours > 0 else None,
            "ci95": [_r(ci[0] * scale), _r(ci[1] * scale)] if ci and scale else None}


def _stats(values: Sequence[float]) -> Optional[dict]:
    if not values:
        return None
    return {"median": _r(statistics.median(values)), "mean": _r(statistics.fmean(values)), "max": _r(max(values)),
            "values": [_r(v) for v in values]}


def _median(values) -> Optional[float]:
    return _r(statistics.median(values)) if values else None


def _span(values: Iterable[float]) -> Optional[list[float]]:
    values = list(values)
    return [_r(min(values)), _r(max(values))] if values else None


def link_subsets(n_links: int, size: int, cap: int, seed: int = 0) -> tuple[list[tuple[int, ...]], bool]:
    """Subsets of ``size`` of the link indices 0..n_links-1: all of them when there are at most ``cap``, else ``cap``
    distinct ones drawn with a generator seeded by (seed, n_links, size). Sorted; True when all were taken."""
    if math.comb(n_links, size) <= cap:
        return list(itertools.combinations(range(n_links), size)), True
    rng = random.Random(f"eval_log:{seed}:{n_links}:{size}")
    chosen: set[tuple[int, ...]] = set()
    while len(chosen) < cap:
        chosen.add(tuple(sorted(rng.sample(range(n_links), size))))
    return sorted(chosen), False


# ---------------------------------------------------------------------------------------------- scoring

@dataclass
class _Episode:
    index: int                        # row number among the labels, from 1
    label: str
    kind: str                         # MOTION_KIND, EMPTY_KIND or OTHER_KIND
    start: float                      # host time
    end: float                        # host time, math.inf for a label that runs to the end of the log
    shown: tuple[float, float]        # start and end in label time, as reported
    zone: Optional[tuple[float, float, float, float]]
    i0: int                           # its frames are i0..i1-1: start <= time < end
    i1: int
    observed: list[float]             # seconds each of those frames stands for inside the episode


@dataclass(frozen=True)
class _EpisodeTally:
    frames: int
    hits: int                         # frames in MOTION
    decided: int                      # frames with a decision
    latency: Optional[float]          # episode start to the first frame in MOTION, None when there is none
    alarms: int                       # rising edges into MOTION inside the episode
    observed_s: float                 # seconds of decided frames inside the episode


@dataclass
class _Tally:
    episodes: list = field(default_factory=list)    # per episode: _EpisodeTally, None when it has no frames
    motion_frames: int = 0
    motion_hits: int = 0
    motion_episodes: int = 0
    detected: int = 0
    latencies: list = field(default_factory=list)
    empty_frames: int = 0
    empty_decided: int = 0
    empty_hits: int = 0
    alarms: int = 0
    observed_s: float = 0.0
    other: dict = field(default_factory=dict)       # label -> [frames, frames in MOTION]


def _tally(codes: bytes, episodes: Sequence[_Episode], times: Sequence[float]) -> _Tally:
    """Count one subject's per-frame codes (NO_DECISION, NO_MOTION, IN_MOTION) over the episodes."""
    out = _Tally()
    for ep in episodes:
        seg = codes[ep.i0:ep.i1]
        if not seg:
            out.episodes.append(None)
            continue
        hits = seg.count(IN_MOTION)
        decided = len(seg) - seg.count(NO_DECISION)
        first = seg.find(IN_MOTION)
        latency = times[ep.i0 + first] - ep.start if first >= 0 else None
        before = codes[ep.i0 - 1:ep.i0] if ep.i0 > 0 else bytes([NO_DECISION])
        alarms = (before + seg).translate(TO_MOTION).count(RISE)
        observed = math.fsum(itertools.compress(ep.observed, seg.translate(TO_DECIDED)))
        out.episodes.append(_EpisodeTally(len(seg), hits, decided, latency, alarms, observed))
        if ep.kind == MOTION_KIND:
            out.motion_frames += len(seg)
            out.motion_hits += hits
            out.motion_episodes += 1
            if latency is not None:
                out.detected += 1
                out.latencies.append(latency)
        elif ep.kind == EMPTY_KIND:
            out.empty_frames += len(seg)
            out.empty_decided += decided
            out.empty_hits += hits
            out.alarms += alarms
            out.observed_s += observed
        else:
            counts = out.other.setdefault(ep.label.lower(), [0, 0])
            counts[0] += len(seg)
            counts[1] += hits
    return out


class _Votes:
    """Per-frame room codes for any subset of links, voted from the distinct (motion, decided) masks of the log."""

    def __init__(self, motion: Sequence[int], decided: Sequence[int]) -> None:
        ids: dict[tuple[int, int], int] = {}
        seq = [ids.setdefault(pair, len(ids)) for pair in zip(motion, decided)]
        self._pairs = list(ids)
        self._seq = bytes(seq) if len(ids) <= 256 else seq

    def codes(self, mask: int, k: int) -> bytes:
        """IN_MOTION where at least ``k`` links of ``mask`` are in MOTION, else NO_MOTION where one of them has a
        decision, else NO_DECISION."""
        table = [IN_MOTION if (m & mask).bit_count() >= k else NO_MOTION if d & mask else NO_DECISION
                 for m, d in self._pairs]
        if isinstance(self._seq, bytes):
            return self._seq.translate(bytes(table).ljust(256, b"\0"))
        return bytes(map(table.__getitem__, self._seq))


def _frame_interval(log: Log) -> float:
    gaps = [b - a for a, b in zip(log.times, log.times[1:]) if b > a]
    if gaps:
        return statistics.median(gaps)
    declared = log.header.get("frame_interval_s")
    return float(declared) if _is_number(declared) and declared > 0 else FALLBACK_FRAME_S


def _durations(times: Sequence[float], frame_s: float) -> list[float]:
    """Seconds each frame stands for: the gap to the next frame, at most MAX_GAP_FRAMES intervals (a stall is not
    observed time); the last frame stands for one interval."""
    cap = MAX_GAP_FRAMES * frame_s
    return [min(b - a, cap) for a, b in zip(times, times[1:])] + [frame_s]


def _episodes(log: Log, labels: Sequence[Label], absolute: bool, motion: set[str], empty: set[str],
              durations: Sequence[float]) -> tuple[list[_Episode], list[str]]:
    offset = 0.0 if absolute else log.t0
    log_end = log.times[-1] + durations[-1]
    episodes, notes = [], []
    for index, lab in enumerate(labels, start=1):
        name = lab.label.lower()
        kind = MOTION_KIND if name in motion else EMPTY_KIND if name in empty else OTHER_KIND
        start = lab.start + offset
        end = math.inf if lab.end is None else lab.end + offset
        i0 = bisect.bisect_left(log.times, start)
        i1 = bisect.bisect_left(log.times, end)
        observed = [min(log.times[j] + durations[j], end) - log.times[j] for j in range(i0, i1)]
        shown = (lab.start, (log_end - offset) if lab.end is None else lab.end)
        zone = lab.zone
        where = f"label {index} (line {lab.line}: {lab.label} {_secs(shown[0])}-{_secs(shown[1])} s)"
        if zone is not None and kind != MOTION_KIND:
            notes.append(f"{where} has a zone, but only motion labels are localised: zone ignored")
            zone = None
        if i0 == i1:
            notes.append(f"{where} has no frames in the log: not scored")
        elif start < log.times[0] or end > log_end:
            notes.append(f"{where} reaches outside the log; only its frames inside the log are scored")
        episodes.append(_Episode(index, lab.label, kind, start, end, shown, zone, i0, i1, observed))
    return episodes, notes


def _no_overlap_message(log: Log, absolute: bool, durations: Sequence[float]) -> str:
    end = log.times[-1] + durations[-1]
    if absolute:
        return (f"none of the labels overlaps the log, which covers host times {log.times[0]:.3f} to {end:.3f} "
                f"({_local_time(log.times[0])} to {_local_time(end)}); are the label times seconds from the first "
                "frame? then leave out --absolute")
    return (f"none of the labels overlaps the log, which covers {_secs(log.times[0] - log.t0)} to "
            f"{_secs(end - log.t0)} s "
            "from its first frame; are the label times host times? then pass --absolute")


def _subject(tally: _Tally, states: Counter, levels: Optional[tuple] = None) -> dict:
    report = {
        "states": dict(sorted(states.items())),
        "motion": {
            "episodes_detected": proportion(tally.detected, tally.motion_episodes),
            "frames_in_motion": proportion(tally.motion_hits, tally.motion_frames),
            "latency_s": _stats(tally.latencies),
        },
        "empty": {
            "false_alarms_per_hour": per_hour(tally.alarms, tally.empty_decided, tally.observed_s),
            "frames_in_motion": proportion(tally.empty_hits, tally.empty_decided),
            "frames": tally.empty_frames,
        },
        "other": {label: proportion(hits, frames) for label, (frames, hits) in sorted(tally.other.items())},
    }
    if levels is not None:
        report["motion"]["level_median"], report["empty"]["level_median"] = levels
    return report


def _level_medians(levels: array, episodes: Sequence[_Episode]) -> tuple[Optional[float], Optional[float]]:
    """Median level (score / threshold) of a link during the motion labels and during the empty labels."""
    picked: dict[str, list[float]] = {MOTION_KIND: [], EMPTY_KIND: []}
    for ep in episodes:
        if ep.kind in picked:
            picked[ep.kind].extend(v for v in levels[ep.i0:ep.i1] if not math.isnan(v))
    return _median(picked[MOTION_KIND]), _median(picked[EMPTY_KIND])


def _episode_entry(tally: Optional[_EpisodeTally], kind: str) -> Optional[dict]:
    if tally is None:
        return None
    entry = {"frames_in_motion": tally.hits, "decided_frames": tally.decided}
    if kind == MOTION_KIND:
        entry.update(detected=tally.latency is not None, latency_s=_r(tally.latency))
    elif kind == EMPTY_KIND:
        entry.update(alarms=tally.alarms, observed_s=_r(tally.observed_s))
    return entry


def _localisation(log: Log, episodes: Sequence[_Episode], min_heat: float) -> tuple[Optional[dict], dict[int, dict]]:
    per_episode: dict[int, dict] = {}
    frames = inside = hot = 0
    distances: list[float] = []
    for ep in episodes:
        if ep.kind != MOTION_KIND or ep.zone is None or ep.i1 == ep.i0:
            continue
        x0, y0, x1, y1 = ep.zone
        ep_inside = ep_hot = 0
        ep_distances = []
        for cell in log.hot[ep.i0:ep.i1]:
            if cell is None or not cell[2] > min_heat:
                continue
            x, y = cell[0], cell[1]
            ep_hot += 1
            ep_inside += x0 <= x <= x1 and y0 <= y <= y1
            ep_distances.append(math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1)))
        n = ep.i1 - ep.i0
        per_episode[ep.index] = {"frames": n, "hot_frames": ep_hot, "inside": ep_inside,
                                 "in_zone": proportion(ep_inside, n), "median_distance_m": _median(ep_distances)}
        frames, inside, hot = frames + n, inside + ep_inside, hot + ep_hot
        distances.extend(ep_distances)
    if not per_episode:
        return None, per_episode
    return {"episodes": len(per_episode), "frames": frames, "hot_frames": hot, "min_heat": min_heat,
            "in_zone": proportion(inside, frames), "in_zone_when_hot": proportion(inside, hot),
            "median_distance_m": _median(distances)}, per_episode


def _curve(votes: _Votes, bits: Sequence[int], names: Sequence[str], k: int, episodes: Sequence[_Episode],
           times: Sequence[float], cap: int, seed: int) -> list[dict]:
    """One point per number of links: k-of-n room votes over link subsets, averaged over the subsets."""
    points = []
    for size in range(1, len(bits) + 1):
        combos, complete = link_subsets(len(bits), size, cap, seed)
        need = min(k, size)
        rows = [([names[i] for i in combo], _tally(votes.codes(sum(1 << bits[i] for i in combo), need), episodes,
                                                   times))
                for combo in combos]
        points.append(_curve_point(size, need, complete, math.comb(len(bits), size), rows))
    return points


def _curve_point(size: int, need: int, complete: bool, possible: int, rows: list) -> dict:
    tallies = [t for _, t in rows]
    count = len(tallies)
    frames, episodes = tallies[0].motion_frames, tallies[0].motion_episodes   # the same for every subset

    def mean(values: Iterable[float]) -> float:
        return math.fsum(values) / count

    hits, detected = mean(t.motion_hits for t in tallies), mean(t.detected for t in tallies)
    alarms, decided = mean(t.alarms for t in tallies), mean(t.empty_decided for t in tallies)
    observed, empty_hits = mean(t.observed_s for t in tallies), mean(t.empty_hits for t in tallies)
    latencies = [x for t in tallies for x in t.latencies]
    return {
        "links": size, "k": need, "subsets": count, "possible": possible, "all_subsets": complete,
        "episodes_detected": {**proportion(detected, episodes),
                              "range": _span(t.detected / episodes for t in tallies) if episodes else None},
        "frames_in_motion": {**proportion(hits, frames),
                             "range": _span(t.motion_hits / frames for t in tallies) if frames else None},
        "median_latency_s": _median(latencies),
        "false_alarms_per_hour": {**per_hour(alarms, decided, observed),
                                  "range": _span(t.alarms * 3600.0 / t.observed_s for t in tallies
                                                 if t.observed_s > 0)},
        "empty_frames_in_motion": proportion(empty_hits, decided),
        "per_subset": [{"links": labels, "episodes_detected": t.detected, "frames_in_motion": t.motion_hits,
                        "alarms": t.alarms, "observed_h": _r(t.observed_s / 3600.0)} for labels, t in rows],
    }


def _health(log: Log, names: Sequence[str]) -> dict:
    out = {}
    for label in names:
        samples = log.health.get(label, {})
        row: dict = {key: _median(samples.get(key)) for key in HEALTH_KEYS}
        for key in GAIN_KEYS:
            values = samples.get(key)
            row[key] = None if not values else {
                "values": [_r(v) for v in sorted(set(values))[:10]],
                "changes": sum(a != b for a, b in zip(values, values[1:]))}
        row["drops"] = log.drops.get(label)
        out[label] = row
    return out


def evaluate(log: Log, labels: Sequence[Label], *, absolute: bool = False,
             motion_labels: Iterable[str] = DEFAULT_MOTION_LABELS, empty_labels: Iterable[str] = DEFAULT_EMPTY_LABELS,
             ks: Iterable[int] = DEFAULT_K, max_subsets: int = DEFAULT_MAX_SUBSETS, seed: int = 0,
             min_heat: float = DEFAULT_MIN_HEAT) -> dict:
    """Score ``log`` against ``labels``; returns the JSON-serialisable result that format_summary prints."""
    motion = {name.strip().lower() for name in motion_labels if name.strip()}
    empty = {name.strip().lower() for name in empty_labels if name.strip()}
    if motion & empty:
        raise EvalError(f"label(s) {', '.join(sorted(motion & empty))} cannot be both motion and empty")
    ks = sorted({int(k) for k in ks})
    if not ks or ks[0] < 1:
        raise EvalError("k must be at least 1")
    if max_subsets < 1:
        raise EvalError("max_subsets must be at least 1")
    frame_s = _frame_interval(log)
    durations = _durations(log.times, frame_s)
    episodes, notes = _episodes(log, labels, absolute, motion, empty, durations)
    if all(ep.i0 == ep.i1 for ep in episodes):
        raise EvalError(_no_overlap_message(log, absolute, durations))

    bits = sorted(range(len(log.links)), key=log.links.__getitem__)   # bit of each link, in label order
    names = [log.links[b] for b in bits]
    votes = _Votes(log.motion, log.decided)
    room_tally = _tally(log.room, episodes, log.times)
    link_tallies = {log.links[b]: _tally(votes.codes(1 << b, 1), episodes, log.times) for b in bits}
    subjects = {ROOM: _subject(room_tally, log.states[ROOM])}
    for label, tally in link_tallies.items():
        subjects[label] = _subject(tally, log.states[label], _level_medians(log.levels[label], episodes))
    localisation, located = _localisation(log, episodes, min_heat)
    episode_rows = [{
        "index": ep.index, "label": ep.label, "kind": ep.kind, "start_s": _r(ep.shown[0]), "end_s": _r(ep.shown[1]),
        "zone": list(ep.zone) if ep.zone else None, "frames": ep.i1 - ep.i0,
        "room": _episode_entry(room_tally.episodes[j], ep.kind),
        "links": {label: _episode_entry(tally.episodes[j], ep.kind) for label, tally in link_tallies.items()},
        "localisation": located.get(ep.index),
    } for j, ep in enumerate(episodes)]
    scaling = [{"k": k, "points": _curve(votes, bits, names, k, episodes, log.times, max_subsets, seed)}
               for k in ks] if bits else []
    health = _health(log, names)

    notes = list(log.notes) + notes
    kinds = Counter(ep.kind for ep in episodes if ep.i1 > ep.i0)
    if not kinds[MOTION_KIND]:
        notes.append(f"no motion labels ({', '.join(sorted(motion))}) with frames: detection is not scored")
    if not kinds[EMPTY_KIND]:
        notes.append(f"no empty labels ({', '.join(sorted(empty))}) with frames: false alarms are not scored")
    if not any(code != NO_DECISION for code in log.room):
        notes.append("the room state never had a decision in this log (it was WARMUP, or the log predates the room "
                     "decision); the scaling curve still votes over the link states")
    if not bits:
        notes.append("the log has no links: no per-link results and no scaling curve")
    if localisation is not None and not localisation["hot_frames"]:
        notes.append(f"no frame of the zone-labelled motion episodes has a heatmap above {min_heat:g}: nothing to "
                     "localise (a cold map, or a log without heatmap cells)")
    for label in names:
        if not any(state in DECIDED for state in log.states[label]):
            notes.append(f"link {label} never had a decision ({', '.join(sorted(log.states[label]))}): it counts as "
                         "a miss in every motion label")
        for key in GAIN_KEYS:
            gain = health[label][key]
            if gain and gain["changes"]:
                notes.append(f"link {label}: {key} changed {gain['changes']} time(s) during the run (a receiver "
                             "reset re-rolls the gain and moves the link's scores; its threshold may no longer fit)")
    span = log.times[-1] - log.times[0]
    return {
        "log": {"path": str(log.path), "frames": len(log.times), "t0": log.t0, "span_s": _r(span),
                "frame_s": _r(frame_s), "source": log.header.get("source"), "mode": log.header.get("mode"),
                "thresholds_source": log.header.get("thresholds_source"), "started": log.header.get("started")},
        "labels": {"rows": len(labels), MOTION_KIND: kinds[MOTION_KIND], EMPTY_KIND: kinds[EMPTY_KIND],
                   OTHER_KIND: kinds[OTHER_KIND]},
        "time_base": "absolute" if absolute else "relative",
        "settings": {"motion_labels": sorted(motion), "empty_labels": sorted(empty), "k": ks,
                     "max_subsets": max_subsets, "seed": seed, "min_heat": min_heat, "confidence": 0.95},
        "links": names,
        "subjects": subjects,
        "episodes": episode_rows,
        "localisation": localisation,
        "scaling": scaling,
        "health": health,
        "notes": notes,
    }


# ---------------------------------------------------------------------------------------------- summary

def _local_time(t: float) -> str:
    try:
        return datetime.fromtimestamp(t).isoformat(sep=" ", timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "?"


def _num(value: Optional[float], digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _secs(value: float) -> str:
    """A label time without trailing zeros: 20, 20.25, 1700000020.5 (never 1.7e+09)."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _count(value: Optional[float]) -> str:
    return "-" if value is None else str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _share(p: dict) -> str:
    """``65.0% [55.3-73.6] 52/80``"""
    if p["rate"] is None:
        return "-"
    lo, hi = p["ci95"]
    return f"{100 * p['rate']:.1f}% [{100 * lo:.1f}-{100 * hi:.1f}] {_count(p['count'])}/{_count(p['total'])}"


def _hourly(value: float) -> str:
    return f"{value:.2f}" if value < 10 else f"{value:.1f}" if value < 100 else f"{value:.0f}"


def _rate(p: dict) -> str:
    """``1.50/h [0.04-8.40] 1 in 0.67 h``"""
    if p["rate"] is None:
        return "-"
    lo, hi = p["ci95"]
    return f"{_hourly(p['rate'])}/h [{_hourly(lo)}-{_hourly(hi)}] {_count(p['count'])} in {p['hours']:.3g} h"


def _range_pct(p: dict) -> str:
    return "" if not p.get("range") else f" ({100 * p['range'][0]:.0f}-{100 * p['range'][1]:.0f}%)"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> list[str]:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    return [(indent + "  ".join(str(c).ljust(w) for c, w in zip(r, widths))).rstrip() for r in [header, *rows]]


def _gain(value: Optional[dict]) -> str:
    if not value:
        return "-"
    shown = "/".join(_count(v) for v in value["values"])
    return shown if not value["changes"] else f"{shown} ({value['changes']} changes)"


def format_summary(result: dict) -> str:
    """The readable report of an ``evaluate`` result."""
    log, subjects, out = result["log"], result["subjects"], []
    out.append(f"log: {log['path']}")
    out.append(f"  {log['frames']} frames over {log['span_s']:.1f} s (one per {log['frame_s']:.2f} s); first frame at "
               f"host time {log['t0']:.3f} ({_local_time(log['t0'])})")
    meta = [f"{name} {log[key]}" for name, key in (("source", "source"), ("mode", "mode"),
                                                   ("thresholds", "thresholds_source")) if log.get(key)]
    if meta:
        out.append("  " + "; ".join(meta))
    labels, settings = result["labels"], result["settings"]
    base = "host times" if result["time_base"] == "absolute" else "seconds from the first frame"
    out.append(f"labels: {labels.get('path', '-')} ({base})")
    out.append(f"  {labels[MOTION_KIND]} motion episode(s) (labels {', '.join(settings['motion_labels'])}), "
               f"{labels[EMPTY_KIND]} empty (labels {', '.join(settings['empty_labels'])}), "
               f"{labels[OTHER_KIND]} other")
    out.append("  rates: value [95% Wilson interval] count/total. Frames are not independent (the detectors smooth "
               "and debounce),")
    out.append("  so their intervals are too narrow: quote the episode counts. level: a link's median score / "
               "threshold (1 = threshold).")
    names = [ROOM, *result["links"]]

    if labels[MOTION_KIND]:
        out += ["", "detection during motion labels (latency = label start to first MOTION)"]
        rows = []
        for name in names:
            m = subjects[name]["motion"]
            lat = m["latency_s"]
            rows.append([name, _share(m["episodes_detected"]), _share(m["frames_in_motion"]),
                         "-" if lat is None else f"{lat['median']:.2f} / {lat['max']:.2f} s",
                         _num(m.get("level_median"), 2) if name != ROOM else ""])
        out += _table(["subject", "episodes detected", "frames in MOTION", "latency median / max", "level"], rows)
    if labels[EMPTY_KIND]:
        out += ["", "false alarms during empty labels (rising edges into MOTION, per hour of decided time)"]
        rows = []
        for name in names:
            e = subjects[name]["empty"]
            rows.append([name, _rate(e["false_alarms_per_hour"]), _share(e["frames_in_motion"]),
                         _num(e.get("level_median"), 2) if name != ROOM else ""])
        out += _table(["subject", "false alarms", "frames in MOTION", "level"], rows)
    others = sorted({label for name in names for label in subjects[name]["other"]})
    if others:
        out += ["", "other labels: share of frames in MOTION"]
        out += _table(["subject", *others], [[name, *(_share(subjects[name]["other"][label])
                                                      if label in subjects[name]["other"] else "-"
                                                      for label in others)] for name in names])

    out += ["", "episodes (room state; links = links in MOTION at least once)"]
    rows = []
    for ep in result["episodes"]:
        room, loc = ep["room"], ep["localisation"]
        if room is None:
            result_text, seen, share = "no frames", "", ""
        else:
            share = f"{100 * room['frames_in_motion'] / ep['frames']:.0f}%"
            links = [entry for entry in ep["links"].values() if entry is not None]
            seen = f"{sum(entry['frames_in_motion'] > 0 for entry in links)}/{len(links)}"
            if ep["kind"] == MOTION_KIND:
                result_text = f"MOTION at +{room['latency_s']:.2f} s" if room["detected"] else "missed"
            elif ep["kind"] == EMPTY_KIND:
                result_text = f"{room['alarms']} false alarm(s)"
            else:
                result_text = ""
        zone = "" if not ep["zone"] else ",".join(f"{v:g}" for v in ep["zone"])
        where = "" if not loc else f"{_share(loc['in_zone'])} ({loc['hot_frames']} hot)"
        span = f"{_secs(ep['start_s'])}-{_secs(ep['end_s'])}"
        rows.append([str(ep["index"]), ep["label"], span, str(ep["frames"]), share, result_text, seen, zone, where])
    out += _table(["#", "label", "start-end s", "frames", "MOTION", "room", "links", "zone", "hottest cell in zone"],
                  rows)

    loc = result["localisation"]
    if loc:
        out += ["", f"heatmap localisation: {loc['episodes']} motion episode(s) with a zone, {loc['frames']} frames; "
                    f"a map is hot when its hottest cell is above {loc['min_heat']:g}"]
        out.append(f"  hottest cell in the zone: {_share(loc['in_zone'])} of all frames, "
                   f"{_share(loc['in_zone_when_hot'])} of the hot frames")
        out.append(f"  median distance from the hottest cell to the zone: {_num(loc['median_distance_m'], 2)} m")

    for curve in result["scaling"]:
        out += ["", f"scaling, k={curve['k']}: room MOTION when at least k of the chosen links are in MOTION (k capped "
                    "at the number of links),",
                "  averaged over link subsets (range over the subsets in parentheses)"]
        rows = []
        for p in curve["points"]:
            fa = p["false_alarms_per_hour"]
            fa_text = "-"
            if fa["rate"] is not None:
                fa_text = f"{_hourly(fa['rate'])}/h [{_hourly(fa['ci95'][0])}-{_hourly(fa['ci95'][1])}]"
            if fa.get("range"):
                fa_text += f" ({_hourly(fa['range'][0])}-{_hourly(fa['range'][1])})"
            subsets = f"{p['subsets']} {'(all)' if p['all_subsets'] else 'of ' + str(p['possible'])}"
            rows.append([str(p["links"]), str(p["k"]), subsets,
                         _share(p["episodes_detected"]) + _range_pct(p["episodes_detected"]),
                         _share(p["frames_in_motion"]) + _range_pct(p["frames_in_motion"]), fa_text])
        out += _table(["links", "k", "subsets", "episodes detected", "frames in MOTION", "false alarms"], rows)

    health = result["health"]
    if health:
        columns = [("pkt_rate", "pkt/s", lambda v: _num(v, 1)), ("seq_rate", "sent/s", lambda v: _num(v, 1)),
                   ("seq_loss", "loss", lambda v: "-" if v is None else f"{100 * v:.1f}%"),
                   ("clip_pct", "clipped", lambda v: "-" if v is None else f"{100 * v:.1f}%"),
                   ("rssi", "rssi", lambda v: _num(v, 0)), ("noise_floor", "noise", lambda v: _num(v, 0)),
                   ("agc_gain", "agc gain", _gain), ("fft_gain", "fft gain", _gain),
                   ("drops", "drops", lambda v: "-" if v is None else str(v))]
        shown = [c for c in columns if any(row.get(c[0]) is not None for row in health.values())]
        if shown:
            out += ["", "link health (medians while the link had signal)"]
            out += _table(["link", *(title for _, title, _ in shown)],
                          [[name, *(fmt(health[name].get(key)) for key, _, fmt in shown)] for name in health])
    if result["notes"]:
        out += ["", "notes:"] + [f"  - {note}" for note in result["notes"]]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- command line

def _names(text: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in text.split(",") if part.strip())
    if not names:
        raise argparse.ArgumentTypeError("give at least one label")
    return names


def _k_values(text: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        values = ()
    if not values or min(values) < 1:
        raise argparse.ArgumentTypeError(f"k must be whole numbers of at least 1, such as 2 or 1,2,3; got {text!r}")
    return values


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, got {text!r}")
    return value


def _heat(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not 0.0 <= value < 1.0:
        raise argparse.ArgumentTypeError(f"must be a number from 0 up to 1, got {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, required=True, metavar="run.jsonl",
                    help="a run written with scripts/dashboard.py --log")
    ap.add_argument("--labels", type=Path, required=True, metavar="labels.csv",
                    help="CSV rows of start_s,end_s,label[,zone]")
    ap.add_argument("--out", type=Path, metavar="result.json", help="also write the full result to this JSON file")
    ap.add_argument("--absolute", action="store_true",
                    help="label times are host times (Unix seconds or ISO 8601), not seconds from the first frame")
    ap.add_argument("--motion-labels", type=_names, default=DEFAULT_MOTION_LABELS, metavar="A,B",
                    help=f"labels scored for detection (default {','.join(DEFAULT_MOTION_LABELS)})")
    ap.add_argument("--empty-labels", type=_names, default=DEFAULT_EMPTY_LABELS, metavar="A,B",
                    help=f"labels scored for false alarms (default {','.join(DEFAULT_EMPTY_LABELS)})")
    ap.add_argument("--k", type=_k_values, default=DEFAULT_K, metavar="K[,K...]",
                    help="scaling curve: links in MOTION that make the room MOTION (default 2, the dashboard's rule); "
                         "several values give one curve each")
    ap.add_argument("--max-subsets", type=_positive_int, default=DEFAULT_MAX_SUBSETS, metavar="N",
                    help="scaling curve: link subsets per point; when there are more, N are drawn with --seed "
                         f"(default {DEFAULT_MAX_SUBSETS})")
    ap.add_argument("--seed", type=int, default=0, help="scaling curve: seed of the subset draw (default 0)")
    ap.add_argument("--min-heat", type=_heat, default=DEFAULT_MIN_HEAT, metavar="V",
                    help="localisation: a map whose hottest cell is at or below V is cold "
                         f"(default {DEFAULT_MIN_HEAT:g})")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        log = read_log(args.log)
        labels = read_labels(args.labels, absolute=args.absolute)
        result = evaluate(log, labels, absolute=args.absolute, motion_labels=args.motion_labels,
                          empty_labels=args.empty_labels, ks=args.k, max_subsets=args.max_subsets, seed=args.seed,
                          min_heat=args.min_heat)
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    result["labels"]["path"] = str(args.labels)
    if args.out is not None:
        try:
            args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {args.out}: {e.strerror or e}", file=sys.stderr)
            return 1
    print(format_summary(result), end="")
    if args.out is not None:
        print(f"\nfull result: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
