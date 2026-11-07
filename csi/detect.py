"""Plain signal-processing detectors on the (host_ts, line) stream.

Motion score: variance over time within a 1 s window (windowed by device
timestamps, so drops do not stretch it), averaged across subcarriers. Motion is
declared above a threshold, with a short debounce so the state does not flicker.

Public API (reused by the viewer and evaluator):
    MotionScorer      push(packet) -> score | None, one packet at a time
    detect            (host_ts, line) stream -> Reading(host_ts, score, state, bpm, confidence, enough_data)
    calibrate         empty stream -> Calibration (threshold), raises CalibrationError
    Calibration.save / load_threshold   small JSON threshold file

Breathing (STILL_PRESENT) comes from ``csi.breathing.estimate_breathing``, which
the evaluator can also call directly on an amplitude window.
MOTION overrides breathing; breathing needs a 15 s window and enough confidence.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

from .breathing import NOT_ENOUGH, WINDOW_S as BREATH_WINDOW_S, BreathingEstimate, estimate_breathing
from .parser import CSIPacket, CSIParser, Stream, parse_stream

EMPTY = "EMPTY"
MOTION = "MOTION"
STILL_PRESENT = "STILL_PRESENT"

WINDOW_S = 1.0
MIN_WINDOW_PACKETS = 5
DEBOUNCE_S = 0.5
BREATH_UPDATE_S = 1.0         # recompute breathing at most this often (device time)


@dataclass(frozen=True)
class Reading:
    host_ts: float
    score: float
    state: str  # debounced
    bpm: Optional[float] = None
    confidence: float = 0.0
    enough_data: bool = False  # False until a full breathing window exists


class CalibrationError(ValueError):
    """Calibration input was unusable (too short or too lossy)."""


@dataclass(frozen=True)
class Calibration:
    threshold: float
    rate_hz: float
    duration_s: float
    loss: float

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")


def load_threshold(path: str | Path) -> float:
    """Read the threshold from a calibration JSON file."""
    return float(json.loads(Path(path).read_text())["threshold"])


def estimate_rate_hz(device_ts_us: Iterable[int]) -> float:
    """Packet rate from the median device-timestamp interval (robust to drops)."""
    diffs = np.diff(np.asarray(list(device_ts_us), dtype=np.float64))
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        raise ValueError("need at least two packets to estimate a rate")
    return 1e6 / float(np.median(diffs))


class MotionScorer:
    """Rolling motion score over the last ``window_s`` of device time."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self._window_us = window_s * 1e6
        self._buf: deque[tuple[int, np.ndarray]] = deque()

    def push(self, pkt: CSIPacket) -> Optional[float]:
        self._buf.append((pkt.device_ts, pkt.amplitude))
        while pkt.device_ts - self._buf[0][0] > self._window_us:
            self._buf.popleft()
        full = pkt.device_ts - self._buf[0][0] >= 0.9 * self._window_us
        if len(self._buf) < MIN_WINDOW_PACKETS or not full:
            return None
        window = np.stack([a for _, a in self._buf])
        return float(window.var(axis=0).mean())


class _Debouncer:
    """Switch state only after a candidate has persisted for ``hold_s``.

    MOTION takes effect immediately (it overrides breathing); leaving MOTION, and
    switching between EMPTY and STILL_PRESENT, are debounced.
    """

    def __init__(self, hold_s: float) -> None:
        self._hold_s = hold_s
        self.state: Optional[str] = None
        self._candidate: Optional[str] = None
        self._since = 0.0

    def update(self, raw: str, ts: float) -> str:
        if raw == MOTION:
            self.state, self._candidate = MOTION, None
            return self.state
        if self.state is None or raw == self.state:
            self.state, self._candidate = self.state or raw, None
        elif raw != self._candidate:
            self._candidate, self._since = raw, ts
        if self._candidate and ts - self._since >= self._hold_s:
            self.state, self._candidate = self._candidate, None
        return self.state


class _BreathingTracker:
    """Keeps the latest breathing window (device time) and re-estimates it periodically."""

    def __init__(self, update_s: float = BREATH_UPDATE_S) -> None:
        self._update_s = update_s
        self._buf: deque[tuple[float, np.ndarray]] = deque()
        self._last_update: Optional[float] = None
        self._estimate: BreathingEstimate = NOT_ENOUGH

    def reset(self) -> None:
        """Forget the window: motion contaminates it, so breathing rebuilds from quiet data."""
        self._buf.clear()
        self._last_update = None
        self._estimate = NOT_ENOUGH

    def push(self, pkt: CSIPacket) -> BreathingEstimate:
        t = pkt.device_ts / 1e6
        self._buf.append((t, pkt.amplitude))
        while t - self._buf[0][0] > BREATH_WINDOW_S + 1.0:
            self._buf.popleft()
        if self._last_update is None or t - self._last_update >= self._update_s:
            self._last_update = t
            window = [(ts, a) for ts, a in self._buf if t - ts <= BREATH_WINDOW_S]
            ts = np.array([w[0] for w in window])
            self._estimate = estimate_breathing(np.stack([w[1] for w in window]), ts)
        return self._estimate


def classify(score: float, bpm: Optional[float], threshold: float) -> str:
    """Raw (undebounced) state: MOTION overrides breathing, breathing overrides EMPTY."""
    if score > threshold:
        return MOTION
    return STILL_PRESENT if bpm is not None else EMPTY


def detect(stream: Stream, threshold: float, parser: Optional[CSIParser] = None,
           debounce_s: float = DEBOUNCE_S) -> Iterator[Reading]:
    """Yield a debounced Reading per packet once a full motion window is available."""
    scorer, breath, debounce = MotionScorer(), _BreathingTracker(), _Debouncer(debounce_s)
    for pkt in parse_stream(stream, parser):
        score = scorer.push(pkt)
        if score is not None and score > threshold:
            breath.reset()
        est = breath.push(pkt)
        if score is not None:
            raw = classify(score, est.bpm, threshold)
            yield Reading(pkt.host_ts, score, debounce.update(raw, pkt.host_ts),
                          est.bpm, est.confidence, est.enough_data)


MIN_CALIBRATION_S = 10.0
MAX_CALIBRATION_LOSS = 0.2
THRESHOLD_PERCENTILE = 99.0
THRESHOLD_MARGIN = 1.5  # threshold = percentile * margin


def packet_loss(device_ts_us: list[int]) -> float:
    """Fraction of packets missing, from device-timestamp gaps vs the median interval."""
    interval = 1e6 / estimate_rate_hz(device_ts_us)
    expected = (device_ts_us[-1] - device_ts_us[0]) / interval + 1
    return max(0.0, 1.0 - len(device_ts_us) / expected)


def calibrate(stream: Stream, parser: Optional[CSIParser] = None) -> Calibration:
    """Learn the motion threshold from an empty-room stream.

    Raises CalibrationError if the input is too short or loses too many packets.
    """
    packets = list(parse_stream(stream, parser))
    if len(packets) < 2:
        raise CalibrationError("input too short: fewer than two packets")
    device_ts = [p.device_ts for p in packets]
    duration = (device_ts[-1] - device_ts[0]) / 1e6
    if duration < MIN_CALIBRATION_S:
        raise CalibrationError(f"input too short: {duration:.1f}s < {MIN_CALIBRATION_S:.0f}s")
    loss = packet_loss(device_ts)
    if loss > MAX_CALIBRATION_LOSS:
        raise CalibrationError(f"input too lossy: {loss:.0%} loss > {MAX_CALIBRATION_LOSS:.0%}")
    scorer = MotionScorer()
    scores = [s for s in (scorer.push(p) for p in packets) if s is not None]
    if not scores:
        raise CalibrationError("input too short: no full 1 s window")
    threshold = float(np.percentile(scores, THRESHOLD_PERCENTILE)) * THRESHOLD_MARGIN
    return Calibration(threshold, estimate_rate_hz(device_ts), duration, loss)
