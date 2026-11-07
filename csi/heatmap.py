"""RF-tomography style heatmap of where motion is likely, from per-link motion scores.

Per link, the motion evidence is a log contrast ``log(score / threshold / reference_ratio)``, clipped at
0. Each threshold is already a high percentile of the quiet score plus a margin, so quiet links sit
well below ratio 1 and contribute nothing; a link at twice its threshold contributes ``log 2`` and one
at ten times contributes ``log 10``. The log keeps weak links proportional to strong ones and stops the
very large simulated (and real) motion ratios from saturating everything.

That contrast is spread over the grid along the sender-receiver segment with a Gaussian falloff of the
cell centre's distance to the segment. The weighted links are summed and passed through
``1 - exp(-sensitivity * sum)``: cells where several links agree add up and come out brighter than
cells on one link. Finally the grid is smoothed in time with exponential rise and decay, so an empty
room settles to cold. All time is host time from the packets, never the wall clock.

Public API:
    link_contrast(score, threshold, reference_ratio) -> float
    cell_centres(cols, rows, width_m, depth_m)        -> (cols*rows, 2) row-major
    segment_falloff(centres, a, b, sigma_m)           -> (cols*rows,) in 0..1
    Heatmap(room, ...)                                per-link falloff maps, smoothing
        .advance(now, rows)   step the grid to host time ``now`` from link rows (LinkMonitor snapshot)
        .values               current grid, flat row-major, 0..1
        .contributing         link labels that currently put heat on the grid
"""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

import numpy as np

from .links import LinkId
from .room import Room, point_segment_distance

COLS = 28
ROWS = 18
FALLOFF_M = 0.35          # Gaussian width of a link's footprint, metres (1 sigma)
SENSITIVITY = 0.2         # gain before the soft saturation; higher lights up weaker evidence more
REFERENCE_RATIO = 1.0     # score/threshold at which a link starts contributing
RISE_TAU_S = 0.3          # time constant of a cell heating up
DECAY_TAU_S = 1.5         # time constant of a cell cooling down
STEP_S = 0.1              # host-time step between smoothing updates while packets arrive
CONTRIBUTING_STATES = frozenset({"EMPTY", "MOTION"})


def link_contrast(score: Optional[float], threshold: Optional[float], reference_ratio: float = REFERENCE_RATIO) -> float:
    """Log contrast of a score over its own threshold, 0 when there is no evidence."""
    if score is None or threshold is None or threshold <= 0 or score <= 0:
        return 0.0
    return max(0.0, math.log(score / threshold / reference_ratio))


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
    """Per-link falloff maps for the room's links, plus the smoothed grid."""

    def __init__(self, room: Room, *, cols: int = COLS, rows: int = ROWS, falloff_m: float = FALLOFF_M,
                 sensitivity: float = SENSITIVITY, reference_ratio: float = REFERENCE_RATIO,
                 rise_tau_s: float = RISE_TAU_S, decay_tau_s: float = DECAY_TAU_S,
                 step_s: float = STEP_S) -> None:
        self.cols, self.rows = cols, rows
        self.falloff_m, self.sensitivity, self.reference_ratio = falloff_m, sensitivity, reference_ratio
        self.rise_tau, self.decay_tau, self.step = rise_tau_s, decay_tau_s, step_s
        self._weights: Mapping[str, float] = dict(room.link_weights)
        centres = cell_centres(cols, rows, room.width_m, room.depth_m)
        self._falloff: dict[str, np.ndarray] = {}
        for rx, tx in room.links():
            label = LinkId(rx.name, tx.mac).label
            self._falloff[label] = segment_falloff(centres, (rx.x, rx.y), (tx.x, tx.y), falloff_m)
        self.values = np.zeros(cols * rows)
        self._last_t: Optional[float] = None
        self.contributing: list[str] = []

    @property
    def known_labels(self) -> set[str]:
        return set(self._falloff)

    def due(self, now: float) -> bool:
        return self._last_t is None or now - self._last_t >= self.step

    def advance(self, now: float, rows: Sequence[Mapping]) -> None:
        """Move the grid to host time ``now`` towards what ``rows`` (link snapshot at ``now``) implies."""
        self.contributing = []
        target = np.zeros_like(self.values)
        for row in rows:
            label = row["id"]
            if label not in self._falloff or row["state"] not in CONTRIBUTING_STATES or row["threshold"] is None:
                continue
            contrast = link_contrast(row["score"], row["threshold"], self.reference_ratio)
            if contrast <= 0:
                continue
            self.contributing.append(label)
            target += self._weights.get(label, 1.0) * contrast * self._falloff[label]
        target = 1.0 - np.exp(-self.sensitivity * target)
        if self._last_t is None or now > self._last_t:
            dt = 0.0 if self._last_t is None else now - self._last_t
            self._smooth(target, dt)
            self._last_t = now
        else:
            self._smooth(target, 0.0)

    def _smooth(self, target: np.ndarray, dt: float) -> None:
        if dt <= 0:
            return
        tau = np.where(target > self.values, self.rise_tau, self.decay_tau)
        self.values = self.values + (target - self.values) * (1.0 - np.exp(-dt / tau))
        self.values = np.clip(self.values, 0.0, 1.0)
