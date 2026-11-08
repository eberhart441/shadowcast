"""Kalman filter that follows one moving person on the heatmap.

Each heatmap step gives at most one measurement: the heat-weighted centroid of the cells within
``hot_fraction`` of the hottest cell, when the hottest cell reaches ``min_heat``. A Kalman filter with
state (x, y, vx, vy) smooths those into one dot that moves continuously and coasts through short gaps.
Velocity decays towards zero with ``velocity_tau_s`` (people in a small room turn often), and the
track is dropped after ``lost_s`` without a measurement. Measurement noise is fixed, not scaled by heat:
on the rig the heat peaks low (median about 0.2), and heat-scaled noise left the dot trailing 0.26 m
behind a walking person and taking 1.3 s to catch up after a jump; fixed 0.25 m trails 0.05 m, 0.5 s.

Updates are rate-limited to one per ``min_step_s`` of host time, so the result does not depend on how
often the page polls.

Limits: one person (two people pull the dot between them). The heatmap only lights up along the links,
so the dot is drawn towards the link lines and their crossings: this smooths the heatmap, it does not
add accuracy the links do not have.

Public API:
    heat_measurement(values, centres, min_heat, hot_fraction) -> (x, y, heat) or None
    Tracker(width_m, depth_m, centres, ...)
        .update(now, values)   step to host time ``now`` with the current heatmap cells
        .state(now)            {x, y, vx, vy, confidence} or None when there is no track
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

MIN_HEAT = 0.1          # hottest cell below this: no measurement
HOT_FRACTION = 0.45     # cells at or above this share of the hottest cell make up the blob
MEAS_SIGMA_M = 0.25     # measurement noise, metres
ACCEL_SIGMA = 1.5       # walking: white acceleration noise, m/s^2
VELOCITY_TAU_S = 1.5    # velocity decays with this time constant
LOST_S = 3.0            # no measurement for this long: drop the track
MIN_STEP_S = 0.1        # at most one update per this much host time
MAX_SPEED_MPS = 2.0     # clamp, so a coasting dot never shoots off


def heat_measurement(values: np.ndarray, centres: np.ndarray, min_heat: float = MIN_HEAT,
                     hot_fraction: float = HOT_FRACTION) -> Optional[tuple[float, float, float]]:
    """(x, y, peak heat) of the hot blob, or None when the map is cold."""
    if len(values) == 0:
        return None
    peak = float(values.max())
    if peak < min_heat:
        return None
    hot = values >= hot_fraction * peak
    w = values[hot]
    x, y = (centres[hot] * w[:, None]).sum(axis=0) / w.sum()
    return float(x), float(y), peak


class Tracker:
    def __init__(self, width_m: float, depth_m: float, centres: np.ndarray, *, min_heat: float = MIN_HEAT,
                 hot_fraction: float = HOT_FRACTION, meas_sigma_m: float = MEAS_SIGMA_M,
                 accel_sigma: float = ACCEL_SIGMA, velocity_tau_s: float = VELOCITY_TAU_S,
                 lost_s: float = LOST_S, min_step_s: float = MIN_STEP_S) -> None:
        self.width_m, self.depth_m, self.centres = width_m, depth_m, centres
        self.min_heat, self.hot_fraction, self.meas_sigma_m = min_heat, hot_fraction, meas_sigma_m
        self.accel_sigma, self.velocity_tau_s = accel_sigma, velocity_tau_s
        self.lost_s, self.min_step_s = lost_s, min_step_s
        self.x: Optional[np.ndarray] = None   # (x, y, vx, vy)
        self.P: Optional[np.ndarray] = None
        self._t: Optional[float] = None
        self._last_meas_t: Optional[float] = None
        self._heat = 0.0
        self.last_measurement: Optional[tuple[float, float, float]] = None  # for evaluation

    def update(self, now: float, values: np.ndarray) -> None:
        if self._t is not None and now - self._t < self.min_step_s:
            return
        dt = 0.0 if self._t is None else now - self._t
        self._t = now
        if self.x is not None:
            self._predict(dt)
        m = heat_measurement(values, self.centres, self.min_heat, self.hot_fraction)
        self.last_measurement = m
        if m is None:
            if self.x is not None and now - self._last_meas_t > self.lost_s:
                self.x = self.P = None
            return
        mx, my, heat = m
        r = self.meas_sigma_m ** 2
        if self.x is None:
            self.x = np.array([mx, my, 0.0, 0.0])
            self.P = np.diag([r, r, 0.5, 0.5])
        else:
            self._correct(np.array([mx, my]), r)
        self._last_meas_t, self._heat = now, heat

    def state(self, now: Optional[float] = None) -> Optional[dict]:
        """The dot, or None. Confidence is the blob's heat, fading with time since the last measurement."""
        if self.x is None:
            return None
        age = 0.0 if now is None else max(0.0, now - self._last_meas_t)
        x, y, vx, vy = (float(v) for v in self.x)
        return {"x": x, "y": y, "vx": vx, "vy": vy, "confidence": self._heat * math.exp(-age / self.lost_s)}

    def _predict(self, dt: float) -> None:
        if dt <= 0:
            return
        decay = math.exp(-dt / self.velocity_tau_s)
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        F[2, 2] = F[3, 3] = decay
        q = self.accel_sigma ** 2
        Q1 = q * np.array([[dt ** 4 / 4, dt ** 3 / 2], [dt ** 3 / 2, dt ** 2]])
        Q = np.zeros((4, 4))
        Q[np.ix_([0, 2], [0, 2])] = Q1
        Q[np.ix_([1, 3], [1, 3])] = Q1
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        self._clamp()

    def _correct(self, z: np.ndarray, r: float) -> None:
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1.0
        S = H @ self.P @ H.T + r * np.eye(2)
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - H @ self.x)
        self.P = (np.eye(4) - K @ H) @ self.P
        self._clamp()

    def _clamp(self) -> None:
        self.x[0] = min(max(self.x[0], 0.0), self.width_m)
        self.x[1] = min(max(self.x[1], 0.0), self.depth_m)
        speed = math.hypot(self.x[2], self.x[3])
        if speed > MAX_SPEED_MPS:
            self.x[2:] *= MAX_SPEED_MPS / speed
