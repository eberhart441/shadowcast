"""Locator: is someone in the room, and roughly where, from each link's raw CSI. No high-pass, no training.

Why not the motion score: the score high-passes each subcarrier at about 1 Hz and averages them. On the rig
(accuracy run 2026-11-08, csi/accuracy.py protocol) that removed most of a person: moving on the spot raised the
best link's score to only 1.5-1.8x its empty-room median, the empty room alone reached 2.4-2.6x, and even a model
trained on that session placed only 22% of 1 s windows at the right mark. Without the high-pass, each subcarrier
measured against its own empty-room variance, presence detection went from ROC AUC 0.82 to 0.98.

Per link and 1 s window (stepped every 0.2 s of host time):
    f = log( p90 over subcarriers of  window variance / that subcarrier's empty-room variance )
with the window's packets first passed through a running median of 3 (one corrupted packet is not a person).
Presence: the strongest link's f above a cut set from the empty room (its p99 plus a margin), with hysteresis.
Position: for every grid cell, the correlation between the links' f and the cell's closeness to each link line,
exp(-excess_path / tau); the best cell wins. Correlating the pattern, not the size, ignores how hard the person
moves. The reported position is the median of the last second of present windows.

Uncertainty, all reported so the page can show it:
    sigma        the spread of the cells that match almost as well as the best (an ellipse), never smaller than the
                 error measured at that spot (config/accuracy_map.json): on the run the true position fell inside it
                 68% of the time (in-sample); the spread alone covered only 7%
    ghost        a second, distant place that matches almost as well (the mirror-twin case)
    maybe_still  a person who stands still is mostly invisible (85% of the still frames on the run): after sustained
                 motion (ARM_STEPS) stops, the state is "maybe_still" at the last position for STILL_HOLD_S, then empty

The empty-room reference comes from the dashboard's calibration clips (reference_from_clips) and is saved next to
the thresholds. While the room has been empty for STILL_HOLD_S it slowly follows the room (ADAPT_TAU_S).
"""
from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

import numpy as np

from .links import LinkId
from .room import Room

WINDOW_S = 1.0
STEP_S = 0.2
MIN_PACKETS = 15            # per link and window
MIN_LINKS = 3               # links with a feature needed to place anyone
PERCENTILE = 90
TAU_M = 0.3                 # closeness kernel: exp(-excess path / TAU_M)
PRESENCE_Q = 99             # empty-room percentile of the strongest link's f ...
PRESENCE_MARGIN = 0.0       # ... plus this (log units) is the presence cut
ENTER_K, ENTER_N = 3, 4     # become present: the cut is passed in 3 of the last 4 steps (0.6 s)
STAY_N = 10                 # stay present while it is passed in any of the last 10 steps (2 s): bridges the gaps
ARM_STEPS = 8               # only a presence with this many steps above the cut (1.6 s) (re)starts the maybe_still hold
# Set on the accuracy run (2026-11-08, replayed): real presence came in runs of median 1.2 s with gaps; the empty
# room's few windows above the cut were isolated. With these rules: no false presence in the empty room, "moving"
# in 88% of frames at the marks and 95% walking; a person standing still: 15% moving, 85% maybe_still.
SMOOTH_S = 1.0
NEAR_BEST = 0.05            # cells within this correlation of the best make up the uncertainty spread
GHOST_GAP = 0.08            # a distant cell this close to the best is reported as a ghost
GHOST_MIN_DIST_M = 0.8
ELLIPSE_SCALE = 1.0         # set from the accuracy run (scripts/accuracy_eval.py --locator)
MIN_SIGMA_M = 0.15          # never claim better than the ground truth could show
STILL_HOLD_S = 60.0
ADAPT_TAU_S = 300.0
COLS, ROWS = 28, 18


class Locator:
    def __init__(self, room: Room, *, cols: int = COLS, rows: int = ROWS, tau_m: float = TAU_M) -> None:
        self._room = room
        xs = (np.arange(cols) + 0.5) * room.width_m / cols
        ys = (np.arange(rows) + 0.5) * room.depth_m / rows
        gx, gy = np.meshgrid(xs, ys)
        self.cells = np.column_stack([gx.ravel(), gy.ravel()])
        self.labels: list[str] = []
        closeness = []
        for rx, tx in room.links():
            a, b = np.array([rx.x, rx.y]), np.array([tx.x, tx.y])
            excess = (np.linalg.norm(self.cells - a, axis=1) + np.linalg.norm(self.cells - b, axis=1)
                      - np.linalg.norm(a - b))
            self.labels.append(LinkId(rx.name, tx.mac).label)
            closeness.append(np.exp(-excess / tau_m))
        self._closeness = np.column_stack(closeness) if closeness else np.zeros((len(self.cells), 0))
        self._buf: dict[str, deque] = {lab: deque() for lab in self.labels}
        self._ref: dict[str, np.ndarray] = {}
        self._cut: Optional[float] = None
        self._last_step: Optional[float] = None
        self._recent: deque = deque()          # (t, x, y) of present windows, for smoothing
        self._above: deque = deque(maxlen=max(ENTER_N, STAY_N))   # recent steps: cut passed or not
        self._present = False
        self._episode_above = 0                # steps above the cut in the current presence
        self._last_present: Optional[float] = None
        self._last_pos: Optional[tuple[float, float]] = None
        self._sigma: Optional[dict] = None
        self._ghost: Optional[dict] = None
        self._accuracy: Optional[np.ndarray] = None   # (n, 3): x, y, measured typical error at that spot
        self._given: Optional[np.ndarray] = None      # (rows, cols): typical error when the dot is shown there
        self._accuracy_note: Optional[str] = None
        self._out: dict = {"calibrated": False, "state": "uncalibrated"}

    # ------------------------------------------------------------------ input

    def push(self, receiver: str, pkt) -> None:
        label = LinkId(receiver, pkt.mac).label
        buf = self._buf.get(label)
        if buf is None or pkt.amplitude is None:
            return
        buf.append((pkt.host_ts, np.asarray(pkt.amplitude, dtype=float)))
        while buf and buf[0][0] < pkt.host_ts - WINDOW_S:
            buf.popleft()

    # ------------------------------------------------------------------ reference

    @property
    def calibrated(self) -> bool:
        return self._cut is not None and len(self._ref) >= MIN_LINKS

    def reference_from_clips(self, clips: Sequence[Iterable[tuple[str, object]]]) -> bool:
        """Learn each link's per-subcarrier empty-room variance and the presence cut from empty-room clips.

        ``clips`` are the dashboard calibration's clips: lists of (receiver, packet). Returns whether enough links
        could be referenced."""
        per_link: dict[str, list[list[tuple[float, np.ndarray]]]] = {}
        for clip in clips:
            seen: dict[str, list[tuple[float, np.ndarray]]] = {}
            for receiver, pkt in clip:
                label = LinkId(receiver, pkt.mac).label
                if label in self._buf and pkt.amplitude is not None:
                    seen.setdefault(label, []).append((pkt.host_ts, np.asarray(pkt.amplitude, dtype=float)))
            for label, rows in seen.items():
                per_link.setdefault(label, []).append(rows)
        refs: dict[str, np.ndarray] = {}
        windows: dict[str, list[np.ndarray]] = {}
        for label, clips_rows in per_link.items():
            wins = []
            for rows in clips_rows:
                n = _dominant_len(rows)
                rows = [(t, a) for t, a in rows if len(a) == n]
                if not rows:
                    continue
                ts = np.array([t for t, _ in rows])
                amps = np.array([a for _, a in rows])
                # overlapping windows stepped like the live locator, so the cut sees as many extremes as it will
                for end in np.arange(ts[0] + WINDOW_S, ts[-1] + 1e-9, STEP_S):
                    w = amps[(ts > end - WINDOW_S) & (ts <= end)]
                    if len(w) >= MIN_PACKETS:
                        wins.append(w)
            if len(wins) >= 25:
                refs[label] = np.median([_window_var(w) for w in wins], axis=0) + 1e-6
                windows[label] = wins
        if len(refs) < MIN_LINKS:
            return False
        # presence cut: the empty room's own windows, scored against the new reference
        maxes = []
        for i in range(min(len(w) for w in windows.values())):
            fs = [_feature(windows[label][i], refs[label]) for label in refs]
            maxes.append(max(fs))
        self._ref = refs
        self._cut = float(np.percentile(maxes, PRESENCE_Q) + PRESENCE_MARGIN)
        self._recent.clear()
        self._above.clear()
        self._present, self._episode_above = False, 0
        self._last_present = self._last_pos = None
        return True

    def set_accuracy_map(self, data: Mapping) -> None:
        """Measured accuracy (config/accuracy_map.json from scripts/accuracy_eval.py): per spot, the typical
        error of this locator there. Near those spots the reported uncertainty never claims to be better."""
        pts = [(m["truth"][0], m["truth"][1], m["err_p68_m"]) for m in data.get("marks", {}).values()
               if m.get("err_p68_m") is not None]
        self._accuracy = np.array(pts, dtype=float) if pts else None
        self._accuracy_note = data.get("note")
        g = data.get("error_given_estimate") or {}
        self._given = np.array(g["p68_m"], dtype=float) if g.get("p68_m") else None

    def measured_error(self, x: float, y: float) -> Optional[float]:
        """How wrong the dot typically was when shown near (x, y) on the accuracy run (68th percentile).

        Uses the error conditioned on the estimate when the map has it (a wrong estimate often lands where the
        system is otherwise good); else inverse-distance weighting of the error measured at each spot."""
        if self._given is not None:
            r, c = self._given.shape
            i = min(r - 1, max(0, int(y / self._room.depth_m * r)))
            j = min(c - 1, max(0, int(x / self._room.width_m * c)))
            return float(self._given[i, j])
        if self._accuracy is None:
            return None
        d = np.hypot(self._accuracy[:, 0] - x, self._accuracy[:, 1] - y) + 0.05
        w = 1 / d ** 2
        return float(np.sum(w * self._accuracy[:, 2]) / np.sum(w))

    def to_json(self) -> dict:
        return {"cut": self._cut, "ref": {k: [float(f"{x:.6g}") for x in v] for k, v in self._ref.items()}}

    def load(self, data: Mapping) -> None:
        self._ref = {k: np.asarray(v, dtype=float) for k, v in data.get("ref", {}).items() if k in self._buf}
        self._cut = data.get("cut")

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_json()))

    # ------------------------------------------------------------------ steps

    def step(self, now: float) -> None:
        if self._last_step is not None and now - self._last_step < STEP_S:
            return
        self._last_step = now
        if not self.calibrated:
            self._out = {"calibrated": False, "state": "uncalibrated"}
            return
        feats, used, variances = [], [], {}
        for i, label in enumerate(self.labels):
            ref = self._ref.get(label)
            rows = [a for t, a in self._buf[label] if t > now - WINDOW_S]
            if ref is None or len(rows) < MIN_PACKETS:
                continue
            n = len(ref)
            w = np.array([a for a in rows if len(a) == n])
            if len(w) < MIN_PACKETS:
                continue
            v = _window_var(w)
            variances[label] = v
            feats.append(_feature_from_var(v, ref))
            used.append(i)
        if len(used) < MIN_LINKS:
            self._out = {"calibrated": True, "state": "no_data", "links_used": len(used)}
            return
        f = np.array(feats)
        level = float(f.max() - self._cut)
        self._above.append(level > 0)
        recent = list(self._above)
        if self._present:
            self._present = any(recent[-STAY_N:])
        else:
            self._present = sum(recent[-ENTER_N:]) >= ENTER_K
        present = self._present
        if not present:
            self._episode_above = 0
        if present and level > 0:
            self._episode_above += 1
            x, y, sigma, ghost = self._locate(f, used)
            self._recent.append((now, x, y))
            self._ghost, self._sigma = ghost, sigma
            if self._episode_above >= ARM_STEPS:   # a short blip neither starts nor extends a minute of maybe_still
                self._last_present = now
        while self._recent and self._recent[0][0] < now - SMOOTH_S:
            self._recent.popleft()
        if self._recent:
            pos = (float(np.median([r[1] for r in self._recent])), float(np.median([r[2] for r in self._recent])))
            self._last_pos = pos
        since = None if self._last_present is None else now - self._last_present
        if present:
            state = "moving"
        elif since is not None and since < STILL_HOLD_S:
            state = "maybe_still"
        else:
            state = "empty"
            self._adapt(variances)
        out = {"calibrated": True, "state": state, "present": bool(present), "level": round(level, 3),
               "links_used": len(used), "since_motion_s": None if since is None else round(since, 1)}
        if state in ("moving", "maybe_still") and self._last_pos is not None:
            sigma = dict(self._sigma) if self._sigma else None
            measured = self.measured_error(*self._last_pos)
            if sigma is not None and measured is not None:   # never claim better than measured here
                sigma["major_m"] = round(max(sigma["major_m"], measured), 3)
                sigma["minor_m"] = round(max(sigma["minor_m"], measured), 3)
            out.update({"x": round(self._last_pos[0], 3), "y": round(self._last_pos[1], 3),
                        "sigma": sigma, "ghost": self._ghost if state == "moving" else None,
                        "measured_error_m": None if measured is None else round(measured, 2)})
        if self._accuracy is not None:
            out["accuracy_map"] = [[round(float(a), 3), round(float(b), 3), round(float(e), 3)]
                                   for a, b, e in self._accuracy]
            out["accuracy_note"] = self._accuracy_note
        self._out = out

    def _locate(self, f: np.ndarray, used: list[int]):
        C = self._closeness[:, used]
        Cz = (C - C.mean(axis=1, keepdims=True)) / (C.std(axis=1, keepdims=True) + 1e-9)
        fz = (f - f.mean()) / (f.std() + 1e-9)
        corr = Cz @ fz / len(used)
        best = int(np.argmax(corr))
        bx, by = self.cells[best]
        near = corr >= corr[best] - NEAR_BEST
        pts = self.cells[near]
        cov = np.cov(pts.T) if len(pts) > 2 else np.zeros((2, 2))
        evals, evecs = np.linalg.eigh(cov + np.eye(2) * 1e-9)
        sx, sy = (max(MIN_SIGMA_M, ELLIPSE_SCALE * math.sqrt(max(e, 0.0))) for e in evals[::-1])
        angle = math.degrees(math.atan2(evecs[1, -1], evecs[0, -1]))
        sigma = {"major_m": round(sx, 3), "minor_m": round(sy, 3), "angle_deg": round(angle, 1)}
        far = np.linalg.norm(self.cells - self.cells[best], axis=1) > GHOST_MIN_DIST_M
        ghost = None
        if far.any():
            j = int(np.flatnonzero(far)[np.argmax(corr[far])])
            if corr[best] - corr[j] < GHOST_GAP:
                ghost = {"x": round(float(self.cells[j][0]), 3), "y": round(float(self.cells[j][1]), 3),
                         "gap": round(float(corr[best] - corr[j]), 3)}
        return float(bx), float(by), sigma, ghost

    def _adapt(self, variances: Mapping[str, np.ndarray]) -> None:
        """A long-empty room: let each reference follow it slowly (temperature, furniture, people outside)."""
        if self._last_present is not None and self._last_step - self._last_present < STILL_HOLD_S:
            return
        a = STEP_S / ADAPT_TAU_S
        for label, v in variances.items():
            if label in self._ref and len(v) == len(self._ref[label]):
                self._ref[label] = (1 - a) * self._ref[label] + a * (v + 1e-6)

    def payload(self) -> dict:
        return dict(self._out)


def _feature_from_var(var: np.ndarray, ref: np.ndarray) -> float:
    return math.log(float(np.percentile(var / ref, PERCENTILE)) + 1e-12)


def _window_var(w: np.ndarray) -> np.ndarray:
    """Per-subcarrier variance over a window of packets (rows, in time order), after a running median of 3.

    The median removes single-packet spikes: on the accuracy run one corrupted packet (20x the noise on most
    subcarriers) lifted a link for exactly the second it stayed in the window, and that read as a person."""
    if len(w) >= 5:
        w = np.median(np.stack([w[:-2], w[1:-1], w[2:]]), axis=0)
    return w.var(axis=0)


def _feature(window: np.ndarray, ref: np.ndarray) -> float:
    return _feature_from_var(_window_var(window), ref)


def _dominant_len(rows: Sequence[tuple[float, np.ndarray]]) -> int:
    lens = [len(a) for _, a in rows]
    return max(set(lens), key=lens.count)
