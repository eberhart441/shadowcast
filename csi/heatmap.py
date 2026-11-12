"""RF-tomography style heatmap of where motion is likely, from per-link motion scores.

Each link is judged against its own quiet baseline, not its threshold. A walking person lifts a real
link to only about 0.6-0.8 of its threshold, so threshold-relative evidence is almost always zero. The
baseline is the median (50th percentile) of that link's own calibrated score over the last 60 s of
host time, so a slow drift in the room is followed while a person moving does not pull it up. It needs
10 s of history before the link contributes at all, so there is no flash at start. The median rather
than a low percentile: on the real rig a noisy link's 20th percentile sits at the bottom of its own
empty-room noise, which makes every spike look like 3x evidence.

Per link, the evidence is the log ratio of score to baseline, mapped to 0..1: ratios up to ``r_floor``
(1.2) are noise and give 0, and ``r_full`` (3x the baseline) gives 1. The evidence times the link's
configured weight is spread over the grid along the sender-receiver segment with a Gaussian falloff of
the cell centre's distance to the segment. Per cell the footprints combine as
``(1 - agreement) * sum + agreement * sqrt(top1 * top2)``, so two links that agree on a cell are hotter
than one link alone, and a lone noisy spike is suppressed. The result goes through
``1 - exp(-sensitivity * combined)``. Finally the grid is smoothed in time with exponential rise and
decay, so an empty room settles to cold. All time is host time from the packets, never the wall clock.

In a room with transceivers (spec #2) the spatial step is radio tomography instead (``imaging="rti"``,
csi/rti.py, ADR-0025): the weighted evidence of every link forms one vector, and one matrix precomputed from the
room's geometry turns it into the image over the grid, so every link that covers a cell counts and more links
sharpen the map. The footprints and the top-2 agreement rule are then not used, and ``falloff_m`` is refused.
The evidence, the soft saturation and the time smoothing are the same in both.

Known limit: the baseline adapts, so a person who stands still for more than half the baseline window
(about 30 s) is absorbed into it and fades. The map shows change in the room, not standing presence.

Public API:
    link_evidence(score, baseline, r_floor, r_full) -> float in 0..1
    QuietBaseline(window_s, percentile, warmup_s)   rolling low percentile of a link's quiet score
    cell_centres(cols, rows, width_m, depth_m)      -> (cols*rows, 2) row-major
    segment_falloff(centres, a, b, sigma_m)         -> (cols*rows,) in 0..1
    Heatmap(room, ..., imaging=None)                per-link falloff maps or the RTI image, baselines, smoothing;
                                                    imaging "rti" when the room has transceivers, else "footprint"
        .advance(now, rows)   step the grid to host time ``now`` from link rows (LinkMonitor snapshot)
        .values               current grid, flat row-major, 0..1
        .target               the last advance's grid before time smoothing (what the tracker reads)
        .contributing         link labels that currently put heat on the grid
        .evidence             {label: 0..1} per link, from the last advance
        .baselines            {label: quiet baseline or None while in warm-up}
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Mapping, Optional, Sequence

import numpy as np

from . import rti
from .links import room_link_ids
from .room import Room, point_segment_distance

COLS = 28
ROWS = 18
FALLOFF_M = 0.35          # Gaussian width of a link's footprint, metres (1 sigma)
SENSITIVITY = 1.3         # gain before the soft saturation; higher lights up weaker evidence more
AGREEMENT = 0.5           # share of the cell value taken from agreement between the two largest links
R_FLOOR = 1.2             # score / baseline at or below which a link's evidence is zero (dead zone)
R_FULL = 3.0              # score / baseline at which a link's evidence reaches 1
BASELINE_WINDOW_S = 60.0  # host-time span of the quiet baseline
BASELINE_PERCENTILE = 50.0
BASELINE_WARMUP_S = 10.0  # history needed before a link's baseline is trusted
BASELINE_RECALC_S = 0.5   # host time between recomputations of a baseline: it spans 60 s, and 870 links need it
RISE_TAU_S = 0.05         # time constant of a cell heating up (0.05 s: the team's pick on the rig)
DECAY_TAU_S = 0.3         # time constant of a cell cooling down
STEP_S = 0.1              # host-time step between smoothing updates while packets arrive
CONTRIBUTING_STATES = frozenset({"EMPTY", "MOTION"})
BASELINE_STATES = frozenset({"EMPTY", "MOTION"})
LIVE_PARAMS = frozenset({"sensitivity", "rise_tau", "decay_tau", "falloff_m"})  # changeable while running
FOOTPRINT, RTI = "footprint", "rti"   # imaging: per-link footprints and top-2 agreement, or radio tomography
FOOTPRINT_ONLY_PARAMS = frozenset({"falloff_m"})


def link_evidence(score: Optional[float], baseline: Optional[float], r_floor: float = R_FLOOR,
                  r_full: float = R_FULL) -> float:
    """Evidence in 0..1 that a link's score is raised above its own quiet baseline.

    The log ratio is zero at ``r_floor`` and one at ``r_full``, clipped. Zero when there is no score or
    no baseline yet.
    """
    if score is None or baseline is None or baseline <= 0 or score <= 0:
        return 0.0
    x = math.log(score / baseline / r_floor) / math.log(r_full / r_floor)
    return min(1.0, max(0.0, x))


class QuietBaseline:
    """A percentile (the median by default) of a link's score over the last ``window_s`` of host time, once
    warmed up. The window is also kept sorted, so the value costs O(1) and a push O(window) memory moves: at
    870 links a step stays within the per-frame budget (tests/test_heatmap_rti.py)."""

    def __init__(self, window_s: float = BASELINE_WINDOW_S, percentile: float = BASELINE_PERCENTILE,
                 warmup_s: float = BASELINE_WARMUP_S, recalc_s: float = 0.0) -> None:
        self._window_s, self._percentile, self._warmup_s = window_s, percentile, warmup_s
        self._recalc_s = recalc_s
        self._samples: deque[tuple[float, float]] = deque()
        self._sorted: list[float] = []

    def push(self, t: float, score: float) -> None:
        self._samples.append((t, score))
        bisect.insort(self._sorted, score)
        while self._samples and self._samples[0][0] < t - self._window_s:
            _, old = self._samples.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]

    def value(self) -> Optional[float]:
        """The baseline, or None until the window holds ``warmup_s`` of history."""
        if not self._samples or self._samples[-1][0] - self._samples[0][0] < self._warmup_s:
            return None
        values = self._sorted                       # numpy's default (linear) percentile, on the sorted window
        rank = self._percentile / 100.0 * (len(values) - 1)
        lo = int(math.floor(rank))
        hi = min(lo + 1, len(values) - 1)
        return float(values[lo] + (values[hi] - values[lo]) * (rank - lo))


def cell_centres(cols: int, rows: int, width_m: float, depth_m: float) -> np.ndarray:
    """Centre of each grid cell in metres, origin top-left, flat row-major (row r, column c at r*cols + c)."""
    xs = (np.arange(cols) + 0.5) * width_m / cols
    ys = (np.arange(rows) + 0.5) * depth_m / rows
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def segment_falloff(centres: np.ndarray, a: Sequence[float], b: Sequence[float], sigma_m: float) -> np.ndarray:
    """Gaussian of each cell's distance to the segment a-b: 1 on the segment, exp(-1/2) at sigma_m."""
    d = point_segment_distance(centres, a, b)
    return np.exp(-d ** 2 / (2 * sigma_m ** 2))


class Heatmap:
    """Per-link falloff maps for the room's links, per-link baselines, plus the smoothed grid."""

    def __init__(self, room: Room, *, cols: int = COLS, rows: int = ROWS, falloff_m: float = FALLOFF_M,
                 sensitivity: float = SENSITIVITY, agreement: float = AGREEMENT,
                 r_floor: float = R_FLOOR, r_full: float = R_FULL,
                 baseline_window_s: float = BASELINE_WINDOW_S,
                 baseline_percentile: float = BASELINE_PERCENTILE,
                 baseline_warmup_s: float = BASELINE_WARMUP_S,
                 rise_tau_s: float = RISE_TAU_S, decay_tau_s: float = DECAY_TAU_S,
                 step_s: float = STEP_S, imaging: Optional[str] = None,
                 excess_path_m: float = rti.EXCESS_PATH_M, corr_m: float = rti.CORR_M,
                 noise_ratio: float = rti.NOISE_RATIO) -> None:
        if imaging is None:
            imaging = RTI if room.transceivers else FOOTPRINT
        if imaging not in (FOOTPRINT, RTI):
            raise ValueError(f"imaging must be {FOOTPRINT!r} or {RTI!r}, not {imaging!r}")
        self.imaging = imaging
        self.excess_path_m, self.corr_m, self.noise_ratio = excess_path_m, corr_m, noise_ratio
        self.cols, self.rows = cols, rows
        self.falloff_m, self.sensitivity, self.agreement = falloff_m, sensitivity, agreement
        self.r_floor, self.r_full = r_floor, r_full
        self.baseline_window_s, self.baseline_percentile = baseline_window_s, baseline_percentile
        self.baseline_warmup_s = baseline_warmup_s
        self.rise_tau, self.decay_tau, self.step = rise_tau_s, decay_tau_s, step_s
        self._weights: Mapping[str, float] = dict(room.link_weights)
        self._room = room
        self.centres = cell_centres(cols, rows, room.width_m, room.depth_m)
        self._falloff: dict[str, np.ndarray] = {}
        self._build_falloff(falloff_m)
        self._column = {label: k for k, label in enumerate(self._falloff)}   # link label -> RTI column
        self._imager = None
        if imaging == RTI:
            ends = np.array([[(rx.x, rx.y), (tx.x, tx.y)] for _, rx, tx in room_link_ids(room)]).reshape(-1, 2, 2)
            self._imager = rti.imager(self.centres, ends, excess_path_m, corr_m, noise_ratio)
        self._baselines: dict[str, QuietBaseline] = {
            label: QuietBaseline(baseline_window_s, baseline_percentile, baseline_warmup_s)
            for label in self._falloff}
        self._fed_until: dict[str, float] = {}
        self.values = np.zeros(cols * rows)
        self.target = np.zeros(cols * rows)  # unsmoothed: the Kalman tracker does its own smoothing
        self._last_t: Optional[float] = None
        self.contributing: list[str] = []
        self.evidence: dict[str, float] = {}
        self.baselines: dict[str, Optional[float]] = {}

    def _build_falloff(self, falloff_m: float) -> None:
        for link_id, rx, tx in room_link_ids(self._room):
            self._falloff[link_id.label] = (None if self.imaging == RTI else
                                            segment_falloff(self.centres, (rx.x, rx.y), (tx.x, tx.y), falloff_m))

    def set_params(self, **kwargs) -> None:
        """Change live tunables: sensitivity, rise_tau, decay_tau, falloff_m (attribute names).

        Only these names are accepted; anything else raises ValueError and nothing is changed. All values
        must be positive finite numbers. A new falloff_m rebuilds the per-link footprints.
        """
        unknown = sorted(set(kwargs) - LIVE_PARAMS)
        if unknown:
            raise ValueError(f"not a live heatmap parameter: {', '.join(unknown)}; "
                             f"allowed: {', '.join(sorted(LIVE_PARAMS))}")
        if self.imaging == RTI and FOOTPRINT_ONLY_PARAMS & set(kwargs):
            raise ValueError(f"{', '.join(sorted(FOOTPRINT_ONLY_PARAMS & set(kwargs)))} applies to the footprint "
                             "heatmap only; this room's heatmap is radio tomography")
        for name, value in kwargs.items():
            if not (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        for name, value in kwargs.items():
            setattr(self, name, float(value))
        if "falloff_m" in kwargs:
            self._build_falloff(self.falloff_m)

    @property
    def known_labels(self) -> set[str]:
        return set(self._falloff)

    def due(self, now: float) -> bool:
        return self._last_t is None or now - self._last_t >= self.step

    def advance(self, now: float, rows: Sequence[Mapping]) -> None:
        """Move the grid to host time ``now`` towards what ``rows`` (link snapshot at ``now``) implies."""
        self.contributing = []
        self.evidence = {}
        self.baselines = {}
        parts = []
        y = np.zeros(len(self._column)) if self.imaging == RTI else None
        for row in rows:
            label = row["id"]
            if label not in self._falloff:
                continue
            baseline = self._update_baseline(label, now, row)
            self.baselines[label] = baseline
            if row["state"] not in CONTRIBUTING_STATES or row["score"] is None or baseline is None:
                continue
            evidence = link_evidence(row["score"], baseline, self.r_floor, self.r_full)
            self.evidence[label] = evidence
            if evidence <= 0:
                continue
            self.contributing.append(label)
            if y is not None:
                y[self._column[label]] = self._weights.get(label, 1.0) * evidence
            else:
                parts.append(self._weights.get(label, 1.0) * evidence * self._falloff[label])
        target = self._combine(parts) if y is None else np.maximum(self._imager.image(y), 0.0)
        target = 1.0 - np.exp(-self.sensitivity * target)
        self.target = target
        if self._last_t is None or now > self._last_t:
            dt = 0.0 if self._last_t is None else now - self._last_t
            self._smooth(target, dt)
            self._last_t = now
        else:
            self._smooth(target, 0.0)

    def _combine(self, parts: Sequence[np.ndarray]) -> np.ndarray:
        """Per cell, ``(1 - agreement) * sum + agreement * geometric mean of the two largest``.

        A lone link at a cell scores ``(1 - agreement)`` of its footprint; two links that agree there
        score more than either alone. A single noisy link therefore lights up less than a real blob.
        """
        if not parts:
            return np.zeros_like(self.values)
        stack = np.vstack(parts)
        field = (1.0 - self.agreement) * stack.sum(axis=0)
        if len(parts) >= 2 and self.agreement > 0:
            top = np.sort(stack, axis=0)
            field = field + self.agreement * np.sqrt(top[-1] * top[-2])
        return field

    def _update_baseline(self, label: str, now: float, row: Mapping) -> Optional[float]:
        """Feed the link's quiet baseline with its newest calibrated score, once per packet time."""
        score = row["score"]
        if row["state"] in BASELINE_STATES and score is not None:
            sample_t = now - (row.get("age_s") or 0.0)
            if sample_t > self._fed_until.get(label, -math.inf):
                self._baselines[label].push(sample_t, score)
                self._fed_until[label] = sample_t
        return self._baselines[label].value()

    def _smooth(self, target: np.ndarray, dt: float) -> None:
        if dt <= 0:
            return
        tau = np.where(target > self.values, self.rise_tau, self.decay_tau)
        self.values = self.values + (target - self.values) * (1.0 - np.exp(-dt / tau))
        self.values = np.clip(self.values, 0.0, 1.0)
