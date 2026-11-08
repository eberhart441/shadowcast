"""Single-person tracker on the heatmap image: a blob measurement per frame, a constant-velocity Kalman
filter, gating, track birth after consistent measurements, coasting, and a 'last seen' marker.

The tracker reads only the heatmap payload (``cols``, ``rows``, ``extents``, ``cells`` row-major 0..1 with row 0
at ``y_min``, and ``contributing``), so it follows whatever estimator painted the image (``heatmap.estimator``:
back-projection or RTI) and works on replays of old logs, without estimator internals. Pure Python: a frame of
28 x 18 cells costs about a millisecond.

Per frame at host time t:

1. Activity. The image counts when its hottest cell is at least ``activity_min`` and at least ``min_links``
   links contribute (one link cannot place a person). A confirmed track keeps measuring down to ``keep_min``.
2. Blobs. Local maxima at or above that level, strongest first. Each grows over the connected cells
   (8-neighbour, only downhill, so it stops in the valley between two peaks) at or above ``blob_fraction`` of its
   own peak that no stronger blob took. Its position is the centroid of those cells weighted by how far each rises
   above that cut, steadier than the hottest cell. Its measurement covariance is ``spread_gain``^2 x the weighted
   spread of the cells plus ``meas_floor_m``^2 (never less than one grid cell) on each axis, so a blob drawn out
   along one link only pins the position across that link.
3. Kalman filter on (x, y, vx, vy): constant velocity, white-noise acceleration ``accel_sigma`` (walking), dt
   from the frame times (any spacing). The track takes the blob with the smallest Mahalanobis distance inside
   the 99 % gate of a 2-dof chi-square (9.21) that is also within ``max_speed`` of the last accepted position
   (plus ``jump_slack_m`` for measurement noise). Estimated speeds above ``max_speed`` are cut back.
4. Birth. With no confirmed track, the strongest blob starts a tentative track; ``birth_hits`` gated measurements
   in a row confirm it (``birth_misses`` missed frames allowed). A tentative track also replaces a confirmed one
   that has gone ``takeover_s`` without a measurement, so a person picked up elsewhere is followed at once.
5. Coasting and loss. Without a measurement the track coasts on its prediction, its velocity decaying with
   ``coast_damping_s``. A mature track (measured over at least ``mature_s``) that goes ``coast_s`` without a
   measurement is lost and becomes a 'last seen' marker at its last measured position, whose uncertainty grows
   at ``lost_growth_mps`` (up to ``lost_sigma_max_m``) and which is dropped after ``lost_hold_s``. A younger
   track, most often a burst of noise on two links, is dropped after ``young_coast_s`` and leaves no marker.
   A new track is shown instead of the marker, and replaces it once it is mature.

Estimates stay inside the image extents plus ``margin_m``.

``Tracker.update(t, heatmap)`` returns the payload, or None until a track is confirmed (and after the marker is
dropped):

    active       True while the track is followed (measured or coasting); False for a 'last seen' marker
    x, y         metres: the estimate, or the last measured position when not active
    sx, sy, rho  1-sigma uncertainty in x and y (metres) and their correlation (-1..1): the 1-sigma ellipse
    vx, vy       m/s (0 when not active)
    age_s        seconds since the track's first measurement
    last_seen_s  seconds since its last measurement (0 when this frame measured it)
    trail        [[t, x, y], ...]: the estimate at each measurement in the last ``trail_s``, host time and
                 metres, oldest first

Time is host time (``status.time_s``). A call less than ``min_step_s`` after the last frame processed (an extra
/api/state request between frames) does not step the filter; a clock that jumps back by more than
``reset_backstep_s`` restarts the tracker.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Optional, Sequence

ACTIVITY_MIN = 0.25      # hottest cell needed to start a track (one link at full evidence shows ~0.38, a crossing 0.6)
KEEP_MIN = 0.15          # ... lower while a confirmed track is followed, so a fading walker is not dropped at once
MIN_LINKS = 2            # contributing links needed: one link cannot place a person along it
BLOB_FRACTION = 0.55     # a blob is its connected cells at or above this share of its own peak
MAX_BLOBS = 4            # strongest blobs considered per frame
SPREAD_GAIN = 1.0        # measurement sigma: this x the blob's weighted spread ...
MEAS_FLOOR_M = 0.15      # ... plus this in quadrature (never less than one grid cell)
ACCEL_SIGMA = 1.0        # m/s^2: white-noise acceleration of a walking person (turns at 1 m/s lag less than at 0.5)
INIT_SPEED_SIGMA = 1.0   # m/s: velocity uncertainty of a new track
GATE_CHI2 = 9.21         # 99 % point of a chi-square with 2 degrees of freedom
MAX_SPEED = 2.5          # m/s: a farther jump from the last accepted position is refused; faster estimates are cut
JUMP_SLACK_M = 0.5       # measurement-noise allowance on that jump
BIRTH_HITS = 3           # gated measurements in a row that confirm a track (0.4 s at 5 frames/s)
BIRTH_MISSES = 1         # missed frames a tentative track survives
BIRTH_GAP_S = 1.0        # ... and the longest it may go unmeasured
TAKEOVER_S = 1.0         # a confirmed tentative track replaces a track unmeasured for this long
COAST_S = 3.0            # unmeasured this long, a mature track is lost and becomes a 'last seen' marker
MATURE_S = 2.0           # a track measured over at least this span is mature
YOUNG_COAST_S = 1.0      # a younger track (most often a noise burst) coasts only this long and leaves no marker
COAST_DAMPING_S = 1.0    # time constant of the velocity decay while coasting
LOST_GROWTH_MPS = 0.02   # the marker's uncertainty grows this fast (a person who stopped is probably still there)
LOST_SIGMA_MAX_M = 1.5   # ... up to this much
LOST_HOLD_S = 300.0      # the marker is dropped after this long
MARGIN_M = 0.3           # estimates stay within the image extents plus this (the page's map margin)
TRAIL_S = 20.0           # trail length, host time
TRAIL_STEP_S = 0.15      # at most one trail point per this much host time
MIN_STEP_S = 0.1         # calls closer than this to the last processed frame do not step the filter
RESET_BACKSTEP_S = 5.0   # a clock that goes back further than this restarts the tracker
DECIMALS = 3
TRAIL_DECIMALS = 2


@dataclass(frozen=True)
class Blob:
    """One heatmap blob as a position measurement: centroid and covariance (metres), peak value, cell count."""
    x: float
    y: float
    var_x: float
    cov_xy: float
    var_y: float
    peak: float
    cells: int


@dataclass(frozen=True)
class Grid:
    """A heatmap image: values row-major (row 0 at y0) over the extents x0..x1, y0..y1 in metres."""
    cols: int
    rows: int
    values: tuple[float, ...]
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cell_w(self) -> float:
        return (self.x1 - self.x0) / self.cols

    @property
    def cell_h(self) -> float:
        return (self.y1 - self.y0) / self.rows


def heatmap_grid(heatmap) -> Optional[Grid]:
    """The image of a heatmap payload, or None when there is no usable image (an empty or malformed payload).

    A cell that is not a finite number reads as 0.
    """
    if not isinstance(heatmap, Mapping):
        return None
    cols, rows, cells, ext = (heatmap.get(k) for k in ("cols", "rows", "cells", "extents"))
    if not (_is_int(cols) and _is_int(rows) and cols > 0 and rows > 0):
        return None
    if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes)) or len(cells) != cols * rows:
        return None
    if not isinstance(ext, Mapping):
        return None
    try:
        x0, y0, x1, y1 = (float(ext[k]) for k in ("x_min", "y_min", "x_max", "y_max"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
        return None
    return Grid(cols, rows, tuple(_cell(v) for v in cells), x0, y0, x1, y1)


def find_blobs(heatmap, *, activity_min: float = ACTIVITY_MIN, min_links: int = MIN_LINKS,
               fraction: float = BLOB_FRACTION, spread_gain: float = SPREAD_GAIN,
               floor_m: float = MEAS_FLOOR_M, max_blobs: int = MAX_BLOBS) -> list[Blob]:
    """The blobs of a heatmap payload as measurements, strongest first; [] when it shows no activity.

    A payload without ``contributing`` is not held to ``min_links``.
    """
    grid = heatmap_grid(heatmap)
    if grid is None:
        return []
    contributing = heatmap.get("contributing")
    if isinstance(contributing, (list, tuple)) and len(contributing) < min_links:
        return []
    return grid_blobs(grid, activity_min=activity_min, fraction=fraction, spread_gain=spread_gain,
                      floor_m=floor_m, max_blobs=max_blobs)


def grid_blobs(grid: Grid, *, activity_min: float = ACTIVITY_MIN, fraction: float = BLOB_FRACTION,
               spread_gain: float = SPREAD_GAIN, floor_m: float = MEAS_FLOOR_M,
               max_blobs: int = MAX_BLOBS) -> list[Blob]:
    """The blobs of an image (see the module doc), strongest first; [] when no cell reaches ``activity_min``."""
    v, cols = grid.values, grid.cols
    if not v or max(v) < activity_min:
        return []
    around = _neighbours(grid.rows, cols)
    peaks = sorted((-value, i) for i, value in enumerate(v)
                   if value >= activity_min and all(value >= v[j] for j in around[i]))
    taken = bytearray(len(v))
    cw, ch = grid.cell_w, grid.cell_h
    floor2 = max(floor_m, cw, ch) ** 2
    gain2 = spread_gain ** 2
    blobs: list[Blob] = []
    for neg_peak, start in peaks:
        if taken[start]:
            continue                       # inside a stronger blob
        peak = -neg_peak
        cut = fraction * peak
        taken[start] = 1
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            for j in around[i]:
                if not taken[j] and cut <= v[j] <= v[i] + 1e-9:   # downhill only: stop in the valley
                    taken[j] = 1
                    stack.append(j)
        points = []
        for i in members:
            r, c = divmod(i, cols)
            points.append((grid.x0 + (c + 0.5) * cw, grid.y0 + (r + 0.5) * ch, max(v[i] - cut, 0.0)))
        total = sum(w for _, _, w in points)
        if total <= 0:                      # every cell exactly at the cut (fraction 1): equal weights
            points = [(x, y, 1.0) for x, y, _ in points]
            total = float(len(points))
        mx = sum(x * w for x, _, w in points) / total
        my = sum(y * w for _, y, w in points) / total
        vxx = sum(w * (x - mx) ** 2 for x, _, w in points) / total
        vyy = sum(w * (y - my) ** 2 for _, y, w in points) / total
        vxy = sum(w * (x - mx) * (y - my) for x, y, w in points) / total
        blobs.append(Blob(mx, my, gain2 * vxx + floor2, gain2 * vxy, gain2 * vyy + floor2, peak, len(members)))
        if len(blobs) >= max_blobs:
            break
    return blobs


class Tracker:
    """Follows one moving person on successive heatmap images (see the module doc)."""

    def __init__(self, *, activity_min: float = ACTIVITY_MIN, keep_min: float = KEEP_MIN,
                 min_links: int = MIN_LINKS, blob_fraction: float = BLOB_FRACTION, max_blobs: int = MAX_BLOBS,
                 spread_gain: float = SPREAD_GAIN, meas_floor_m: float = MEAS_FLOOR_M,
                 accel_sigma: float = ACCEL_SIGMA, init_speed_sigma: float = INIT_SPEED_SIGMA,
                 gate_chi2: float = GATE_CHI2, max_speed: float = MAX_SPEED, jump_slack_m: float = JUMP_SLACK_M,
                 birth_hits: int = BIRTH_HITS, birth_misses: int = BIRTH_MISSES, birth_gap_s: float = BIRTH_GAP_S,
                 takeover_s: float = TAKEOVER_S, coast_s: float = COAST_S, mature_s: float = MATURE_S,
                 young_coast_s: float = YOUNG_COAST_S, coast_damping_s: float = COAST_DAMPING_S,
                 lost_growth_mps: float = LOST_GROWTH_MPS, lost_sigma_max_m: float = LOST_SIGMA_MAX_M,
                 lost_hold_s: float = LOST_HOLD_S, margin_m: float = MARGIN_M, trail_s: float = TRAIL_S,
                 trail_step_s: float = TRAIL_STEP_S, min_step_s: float = MIN_STEP_S,
                 reset_backstep_s: float = RESET_BACKSTEP_S) -> None:
        self.activity_min, self.keep_min, self.min_links = activity_min, min(keep_min, activity_min), min_links
        self.blob_fraction, self.max_blobs = blob_fraction, max_blobs
        self.spread_gain, self.meas_floor_m = spread_gain, meas_floor_m
        self.accel_sigma, self.init_speed_sigma = accel_sigma, init_speed_sigma
        self.gate_chi2, self.max_speed, self.jump_slack_m = gate_chi2, max_speed, jump_slack_m
        self.birth_hits, self.birth_misses, self.birth_gap_s = birth_hits, birth_misses, birth_gap_s
        self.takeover_s, self.coast_s, self.coast_damping_s = takeover_s, coast_s, coast_damping_s
        self.mature_s, self.young_coast_s = mature_s, min(young_coast_s, coast_s)
        self.lost_growth_mps, self.lost_sigma_max_m, self.lost_hold_s = lost_growth_mps, lost_sigma_max_m, lost_hold_s
        self.margin_m, self.trail_s, self.trail_step_s = margin_m, trail_s, trail_step_s
        self.min_step_s, self.reset_backstep_s = min_step_s, reset_backstep_s
        self.reset()

    def reset(self) -> None:
        """Forget every track and marker."""
        self._track: Optional[_Track] = None
        self._tentative: Optional[_Track] = None
        self._lost: Optional[_Lost] = None
        self._last_t: Optional[float] = None
        self._bounds: Optional[tuple[float, float, float, float]] = None

    def update(self, t, heatmap) -> Optional[dict]:
        """Step to host time ``t`` with one heatmap payload; return the track payload (None while there is none).

        Never raises: the track is an overlay on the live dashboard, so an unexpected failure resets the tracker
        and gives None rather than ending the run.
        """
        try:
            return self._update(t, heatmap)
        except Exception:
            self.reset()
            return None

    def _update(self, t, heatmap) -> Optional[dict]:
        try:
            t = float(t)
        except (TypeError, ValueError):
            return self.payload(self._last_t)
        if not math.isfinite(t):
            return self.payload(self._last_t)
        if self._last_t is not None:
            if t < self._last_t - self.reset_backstep_s:
                self.reset()                                   # a new clock (a restarted source)
            elif t < self._last_t + self.min_step_s:
                return self.payload(max(t, self._last_t))     # between frames: ages only
        grid = heatmap_grid(heatmap)
        blobs: list[Blob] = []
        if grid is not None:
            m = self.margin_m
            self._bounds = (grid.x0 - m, grid.y0 - m, grid.x1 + m, grid.y1 + m)
            contributing = heatmap.get("contributing")
            if not isinstance(contributing, (list, tuple)) or len(contributing) >= self.min_links:
                blobs = grid_blobs(grid, activity_min=self.keep_min if self._track else self.activity_min,
                                   fraction=self.blob_fraction, spread_gain=self.spread_gain,
                                   floor_m=self.meas_floor_m, max_blobs=self.max_blobs)
        self._step(t, blobs)
        self._last_t = t
        return self.payload(t)

    def payload(self, now: Optional[float]) -> Optional[dict]:
        """The track as of host time ``now`` (see the module doc), or None."""
        if now is None:
            return None
        track = self._track
        if track is not None:
            x, y, vx, vy = track.filt.s
            p = track.filt.p
            return self._format(True, x, y, p[0][0], p[0][1], p[1][1], vx, vy, track, now)
        lost = self._lost
        if lost is None or now - lost.last_hit_t > self.lost_hold_s:
            return None
        grow = min(self.lost_growth_mps * max(0.0, now - lost.last_hit_t), self.lost_sigma_max_m) ** 2
        return self._format(False, lost.x, lost.y, lost.var_x + grow, lost.cov_xy, lost.var_y + grow, 0.0, 0.0,
                            lost, now)

    # ---------------------------------------------------------------- steps

    def _step(self, t: float, blobs: list[Blob]) -> None:
        free = list(blobs)
        if self._lost is not None and t - self._lost.last_hit_t > self.lost_hold_s:
            self._lost = None
        track = self._track
        if track is not None:
            mature = track.last_hit_t - track.first_t >= self.mature_s
            if t - track.last_hit_t >= (self.coast_s if mature else self.young_coast_s):
                self._lose(track, marker=mature)
                track = None
        if track is not None:
            k = self._associate(track, t, free)
            if k is None:
                track.filt.coast(t, self.accel_sigma, self.coast_damping_s)
                self._settle(track)
            else:
                self._hit(track, t, free.pop(k))
        self._step_tentative(t, [b for b in free if b.peak >= self.activity_min])
        tentative = self._tentative
        if tentative is not None and tentative.hits >= self.birth_hits and (
                self._track is None or t - self._track.last_hit_t >= self.takeover_s):
            self._track, self._tentative = tentative, None
        track = self._track
        if track is not None and self._lost is not None and track.last_hit_t - track.first_t >= self.mature_s:
            self._lost = None          # a mature track supersedes the old marker; a young one may still be noise

    def _step_tentative(self, t: float, blobs: list[Blob]) -> None:
        tentative = self._tentative
        if tentative is not None and t - tentative.last_hit_t > self.birth_gap_s:
            self._tentative = tentative = None
        if tentative is not None:
            k = self._associate(tentative, t, blobs)
            if k is not None:
                self._hit(tentative, t, blobs.pop(k))
                return
            tentative.misses += 1
            if tentative.misses <= self.birth_misses:
                return
            self._tentative = None
        if blobs:
            self._tentative = self._start(t, blobs[0])

    def _start(self, t: float, blob: Blob) -> "_Track":
        filt = _Filter(t, blob, self.init_speed_sigma)
        track = _Track(filt, t, t, blob.x, blob.y, (blob.var_x, blob.cov_xy, blob.var_y),
                       deque(maxlen=int(self.trail_s / max(self.trail_step_s, 0.01)) + 2))
        self._settle(track)
        track.hit_x, track.hit_y = filt.s[0], filt.s[1]
        track.trail.append((t, filt.s[0], filt.s[1]))
        return track

    def _associate(self, track: "_Track", t: float, blobs: Sequence[Blob]) -> Optional[int]:
        """Index of the blob the track takes at ``t``: the nearest (Mahalanobis) inside both gates, or None."""
        if not blobs:
            return None
        s, p = track.filt.predict(t, self.accel_sigma)
        reach = self.max_speed * max(0.0, t - track.last_hit_t) + self.jump_slack_m
        best: Optional[tuple[float, int]] = None
        for k, blob in enumerate(blobs):
            if math.hypot(blob.x - track.hit_x, blob.y - track.hit_y) > reach:
                continue
            d2 = _mahalanobis2(s, p, blob)
            if d2 is not None and d2 <= self.gate_chi2 and (best is None or d2 < best[0]):
                best = (d2, k)
        return None if best is None else best[1]

    def _hit(self, track: "_Track", t: float, blob: Blob) -> None:
        if not track.filt.update(t, blob, self.accel_sigma):
            return
        s = track.filt.s
        speed = math.hypot(s[2], s[3])
        if speed > self.max_speed:
            s[2], s[3] = s[2] * self.max_speed / speed, s[3] * self.max_speed / speed
        self._settle(track)
        p = track.filt.p
        track.last_hit_t, track.hit_x, track.hit_y = t, s[0], s[1]
        track.hit_cov = (p[0][0], p[0][1], p[1][1])
        track.hits += 1
        track.misses = 0
        trail = track.trail
        if trail and t - trail[-1][0] < self.trail_step_s:
            trail[-1] = (trail[-1][0], s[0], s[1])
        else:
            trail.append((t, s[0], s[1]))
        while trail and trail[0][0] < t - self.trail_s:
            trail.popleft()

    def _settle(self, track: "_Track") -> None:
        """Keep the estimate inside the bounds (no velocity out of them), or drop the track if it went non-finite."""
        filt = track.filt
        if not filt.finite():
            if track is self._track:
                self._track = None
            if track is self._tentative:
                self._tentative = None
            return
        if self._bounds is None:
            return
        x0, y0, x1, y1 = self._bounds
        s = filt.s
        for axis, lo, hi in ((0, x0, x1), (1, y0, y1)):
            if s[axis] < lo:
                s[axis], s[axis + 2] = lo, max(0.0, s[axis + 2])
            elif s[axis] > hi:
                s[axis], s[axis + 2] = hi, min(0.0, s[axis + 2])

    def _lose(self, track: "_Track", marker: bool) -> None:
        """Drop the track; a mature one leaves its 'last seen' marker (a young one leaves any older marker)."""
        if marker:
            var_x, cov_xy, var_y = track.hit_cov
            self._lost = _Lost(track.hit_x, track.hit_y, var_x, cov_xy, var_y, track.first_t, track.last_hit_t,
                               track.trail)
        self._track = None

    def _format(self, active: bool, x: float, y: float, var_x: float, cov_xy: float, var_y: float, vx: float,
                vy: float, source, now: float) -> dict:
        sx, sy = math.sqrt(max(var_x, 0.0)), math.sqrt(max(var_y, 0.0))
        rho = cov_xy / (sx * sy) if sx > 0 and sy > 0 else 0.0
        since = now - self.trail_s
        return {
            "active": active,
            "x": _round(x), "y": _round(y),
            "sx": _round(sx), "sy": _round(sy), "rho": _round(max(-1.0, min(1.0, rho))),
            "vx": _round(vx), "vy": _round(vy),
            "age_s": _round(max(0.0, now - source.first_t)),
            "last_seen_s": _round(max(0.0, now - source.last_hit_t)),
            "trail": [[round(pt, TRAIL_DECIMALS), round(px, TRAIL_DECIMALS), round(py, TRAIL_DECIMALS)]
                      for pt, px, py in source.trail if pt >= since],
        }


# -------------------------------------------------------------------- internals

class _Filter:
    """Constant-velocity Kalman filter on (x, y, vx, vy) with white-noise acceleration, at time ``t``."""

    def __init__(self, t: float, blob: Blob, speed_sigma: float) -> None:
        sv = speed_sigma ** 2
        self.t = t
        self.s = [blob.x, blob.y, 0.0, 0.0]
        self.p = [[blob.var_x, blob.cov_xy, 0.0, 0.0],
                  [blob.cov_xy, blob.var_y, 0.0, 0.0],
                  [0.0, 0.0, sv, 0.0],
                  [0.0, 0.0, 0.0, sv]]

    def predict(self, t: float, accel_sigma: float) -> tuple[list[float], list[list[float]]]:
        """State and covariance predicted to ``t`` (not stored)."""
        dt = max(0.0, t - self.t)
        x, y, vx, vy = self.s
        f = [[1.0, 0.0, dt, 0.0], [0.0, 1.0, 0.0, dt], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
        q = accel_sigma ** 2
        a, b, c = q * dt ** 3 / 3.0, q * dt ** 2 / 2.0, q * dt   # integrated white-noise acceleration: any dt
        noise = [[a, 0.0, b, 0.0], [0.0, a, 0.0, b], [b, 0.0, c, 0.0], [0.0, b, 0.0, c]]
        return [x + vx * dt, y + vy * dt, vx, vy], _plus(_mul(_mul(f, self.p), _transpose(f)), noise)

    def update(self, t: float, blob: Blob, accel_sigma: float) -> bool:
        """Predict to ``t`` and take the measurement (Joseph form). False, unchanged, if it is degenerate."""
        s, p = self.predict(t, accel_sigma)
        r = [[blob.var_x, blob.cov_xy], [blob.cov_xy, blob.var_y]]
        inv = _inverse2([[p[0][0] + r[0][0], p[0][1] + r[0][1]], [p[1][0] + r[1][0], p[1][1] + r[1][1]]])
        if inv is None:
            return False
        gain = [[p[i][0] * inv[0][0] + p[i][1] * inv[1][0], p[i][0] * inv[0][1] + p[i][1] * inv[1][1]]
                for i in range(4)]
        nu = (blob.x - s[0], blob.y - s[1])
        self.s = [s[i] + gain[i][0] * nu[0] + gain[i][1] * nu[1] for i in range(4)]
        ikh = [[(1.0 if i == j else 0.0) - (gain[i][j] if j < 2 else 0.0) for j in range(4)] for i in range(4)]
        joseph = _plus(_mul(_mul(ikh, p), _transpose(ikh)), _mul(_mul(gain, r), _transpose(gain)))
        self.p = [[(joseph[i][j] + joseph[j][i]) / 2.0 for j in range(4)] for i in range(4)]
        self.t = t
        return True

    def coast(self, t: float, accel_sigma: float, damping_s: float) -> None:
        """Move to ``t`` without a measurement. The velocity decays with time constant ``damping_s`` (an
        Ornstein-Uhlenbeck velocity driven by the same white-noise acceleration), so a coasting estimate drifts
        at most about speed x ``damping_s`` and its uncertainty grows like a random walk, not like dt^3."""
        if damping_s <= 0:
            self.s, self.p = self.predict(t, accel_sigma)
            self.t = t
            return
        dt, tau, q = max(0.0, t - self.t), damping_s, accel_sigma ** 2
        decay = math.exp(-dt / tau)
        reach = tau * (1.0 - decay)
        x, y, vx, vy = self.s
        self.s = [x + vx * reach, y + vy * reach, vx * decay, vy * decay]
        f = [[1.0, 0.0, reach, 0.0], [0.0, 1.0, 0.0, reach], [0.0, 0.0, decay, 0.0], [0.0, 0.0, 0.0, decay]]
        a = max(0.0, q * tau ** 2 * (dt - 2.0 * tau * (1.0 - decay) + tau * (1.0 - decay ** 2) / 2.0))
        b = q * tau ** 2 * (1.0 - decay) ** 2 / 2.0
        c = q * tau * (1.0 - decay ** 2) / 2.0
        noise = [[a, 0.0, b, 0.0], [0.0, a, 0.0, b], [b, 0.0, c, 0.0], [0.0, b, 0.0, c]]
        self.p = _plus(_mul(_mul(f, self.p), _transpose(f)), noise)
        self.t = t

    def finite(self) -> bool:
        return all(math.isfinite(v) for v in self.s) and all(math.isfinite(v) for row in self.p for v in row)


class _Track:
    __slots__ = ("filt", "first_t", "last_hit_t", "hit_x", "hit_y", "hit_cov", "trail", "hits", "misses")

    def __init__(self, filt: _Filter, first_t: float, last_hit_t: float, hit_x: float, hit_y: float,
                 hit_cov: tuple[float, float, float], trail: deque) -> None:
        self.filt, self.first_t, self.last_hit_t = filt, first_t, last_hit_t
        self.hit_x, self.hit_y, self.hit_cov, self.trail = hit_x, hit_y, hit_cov, trail
        self.hits, self.misses = 1, 0


@dataclass(frozen=True)
class _Lost:
    """A lost track: where and when it was last measured, and its uncertainty then."""
    x: float
    y: float
    var_x: float
    cov_xy: float
    var_y: float
    first_t: float
    last_hit_t: float
    trail: deque


@lru_cache(maxsize=8)
def _neighbours(rows: int, cols: int) -> tuple[tuple[int, ...], ...]:
    """For each cell of a rows x cols grid (row-major), the indices of its up-to-8 neighbours."""
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append(tuple(rr * cols + cc for rr in (r - 1, r, r + 1) for cc in (c - 1, c, c + 1)
                             if (rr, cc) != (r, c) and 0 <= rr < rows and 0 <= cc < cols))
    return tuple(out)


def _mahalanobis2(s: Sequence[float], p: Sequence[Sequence[float]], blob: Blob) -> Optional[float]:
    inv = _inverse2([[p[0][0] + blob.var_x, p[0][1] + blob.cov_xy], [p[1][0] + blob.cov_xy, p[1][1] + blob.var_y]])
    if inv is None:
        return None
    dx, dy = blob.x - s[0], blob.y - s[1]
    return dx * (inv[0][0] * dx + inv[0][1] * dy) + dy * (inv[1][0] * dx + inv[1][1] * dy)


def _inverse2(m: Sequence[Sequence[float]]) -> Optional[list[list[float]]]:
    det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
    if not math.isfinite(det) or det <= 1e-12:
        return None
    return [[m[1][1] / det, -m[0][1] / det], [-m[1][0] / det, m[0][0] / det]]


def _mul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))] for i in range(len(a))]


def _transpose(a: Sequence[Sequence[float]]) -> list[list[float]]:
    return [list(col) for col in zip(*a)]


def _plus(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[x + y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cell(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _round(value: float) -> float:
    return round(float(value), DECIMALS)
