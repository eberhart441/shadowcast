"""Plain signal-processing detectors on the (host_ts, line) stream.

Motion score: per subcarrier, the slow component (below about 1 Hz, which covers
breathing) is removed with a cascade of device-time high-pass stages; the score is
the variance of that residual over a 1 s window (windowed by device timestamps, so
drops do not stretch it), averaged across subcarriers. So chest movement does not
read as motion, and motion means gross movement. Motion is declared above a
threshold, with a short debounce so the state does not flicker.

Public API (reused by the viewer and evaluator):
    MotionScorer      push(packet) -> score | None, one packet at a time
    detect            (host_ts, line) stream -> Reading(host_ts, score, state, bpm, confidence, enough_data)
    calibrate         empty stream -> Calibration (threshold), raises CalibrationError
    Calibration.save / load_threshold   small JSON threshold file

Motion-only is the default: states are EMPTY and MOTION, and STILL_PRESENT is never
emitted. Breathing is opt-in (``breathing=True``) and is not computed otherwise. The
breathing detector (``csi.breathing.estimate_breathing``) was false-firing on empty
rooms on real hardware, so it stays off until it is trusted. With it on, MOTION
overrides breathing; breathing needs a 15 s window and enough confidence.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import numpy as np

from .clock import RESET, DeviceClock
from .breathing import NOT_ENOUGH, WINDOW_S as BREATH_WINDOW_S, BreathingEstimate, estimate_breathing
from .parser import CSIPacket, CSIParser, Stream, parse_stream

EMPTY = "EMPTY"
MOTION = "MOTION"
STILL_PRESENT = "STILL_PRESENT"

SIM_THRESHOLD = 1.0           # simulator default: simulated empty scores ~0.2, motion ~3-9
DEFAULT_MARGIN = 1.25         # calibrated threshold = margin x p99 of empty windows (1.0 = the p99 itself)
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


def validate_margin(margin: float) -> float:
    """Return ``margin`` if it is a usable calibration margin (a finite number above zero), else raise."""
    if not margin > 0 or margin == float("inf"):  # also rejects NaN
        raise ValueError(f"calibration margin must be a positive number, got {margin!r}")
    return float(margin)


class CalibrationError(ValueError):
    """Calibration input was unusable (too short, too sparse, too lossy or too contaminated)."""


@dataclass(frozen=True)
class Calibration:
    threshold: float
    rate_hz: float            # nominal send rate used for loss (given, or estimated)
    duration_s: float         # total clip time
    loss: float
    clean_s: float = 0.0      # seconds of kept (quiet) data the threshold came from
    rejected_s: float = 0.0   # seconds rejected as movement
    pkt_rate_hz: float = 0.0  # effective received packet rate
    clips: int = 1
    margin: float = DEFAULT_MARGIN  # threshold = margin * p99 of the kept windows
    noisy: bool = False             # noisy-room calibration: threshold = margin * p97 of all windows
    warning: str = ""               # noisy mode: how much of the clip looked like movement

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")


def load_threshold(path: str | Path) -> float:
    """Read the threshold from a calibration JSON file."""
    return float(json.loads(Path(path).read_text())["threshold"])


def estimate_rate_hz(device_ts_us: Iterable[int]) -> float:
    """Send rate from device-timestamp intervals, robust to drops.

    Drops make some intervals 2x, 3x... the send period, so the median can sit on a multiple when
    loss is high. Instead, take the lower decile as the near-period interval and average the intervals
    that are close to it.
    """
    diffs = np.diff(np.asarray(list(device_ts_us), dtype=np.float64))
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        raise ValueError("need at least two packets to estimate a rate")
    near = float(np.percentile(diffs, 10))
    return 1e6 / float(np.median(diffs[diffs <= 1.5 * near]))


HIGHPASS_TAU_S = 0.16   # time constant of each slow-component stage (device time)
HIGHPASS_STAGES = 3     # cascade: breathing is attenuated ~21 dB at 0.5 Hz, ~32 dB at 0.3 Hz


class MotionScorer:
    """Rolling motion score over the last ``window_s`` of device time.

    Each packet's amplitude vector is high-passed per subcarrier first: subtract a
    running mean (exponential, time constant ``tau_s`` in device time), and repeat
    ``stages`` times. The slow component (breathing and slow drift) is removed, so
    only faster variation, i.e. gross movement, contributes to the variance.
    """

    def __init__(self, window_s: float = WINDOW_S, tau_s: float = HIGHPASS_TAU_S,
                 stages: int = HIGHPASS_STAGES) -> None:
        self._window_us = window_s * 1e6
        self._tau_s = tau_s
        self._stages = stages
        self._buf: deque[tuple[int, np.ndarray]] = deque()
        self._slow: Optional[np.ndarray] = None  # (stages, subcarriers) running means
        self._last_t: Optional[int] = None       # device time (us) of the last accepted packet
        self._score: Optional[float] = None
        self._clock = DeviceClock()  # unwraps the 32-bit counter; a reboot restarts the window
        self.dropped = 0                         # out-of-order or repeated packets, not applied

    def _highpass(self, x: np.ndarray, t: int) -> np.ndarray:
        x = x.astype(np.float64)
        if self._slow is None:
            # Start the first mean at the first sample and the rest at zero, since
            # each later stage filters a residual that begins at zero. No transient.
            self._slow = np.zeros((self._stages, x.size))
            self._slow[0] = x
            self._last_t = t
            return np.zeros_like(x)
        dt_s = max(0, t - self._last_t) / 1e6  # gaps (drops) advance the mean; accepted times only move forward
        self._last_t = t
        alpha = 1.0 - np.exp(-dt_s / self._tau_s)
        for k in range(self._stages):
            self._slow[k] += alpha * (x - self._slow[k])
            x = x - self._slow[k]
        return x

    def push(self, pkt: CSIPacket) -> Optional[float]:
        """Score after this packet. A packet not later than the last accepted one (out of order, or repeated) is
        dropped and counted: it would move the timeline backwards. Its call returns the unchanged score."""
        t, event = self._clock.advance(pkt.device_ts, pkt.host_ts)
        if event == RESET:
            # Receiver rebooted: the old window and filter state belong to another timeline.
            self._buf.clear()
            self._slow = None
            self._last_t = None
        if self._last_t is not None and t <= self._last_t:
            self.dropped += 1
            return self._score
        self._buf.append((t, self._highpass(pkt.amplitude, t)))
        while t - self._buf[0][0] > self._window_us:
            self._buf.popleft()
        full = t - self._buf[0][0] >= 0.9 * self._window_us
        if len(self._buf) < MIN_WINDOW_PACKETS or not full:
            self._score = None
        else:
            window = np.stack([r for _, r in self._buf])
            self._score = float(window.var(axis=0).mean())
        return self._score


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
        self._clock = DeviceClock()
        self._last_t: Optional[float] = None  # seconds, last accepted packet
        self.dropped = 0                      # out-of-order or repeated packets, not applied

    def reset(self) -> None:
        """Forget the window: motion contaminates it, so breathing rebuilds from quiet data."""
        self._buf.clear()
        self._last_update = None
        self._estimate = NOT_ENOUGH
        self._last_t = None

    def push(self, pkt: CSIPacket) -> BreathingEstimate:
        t_us, event = self._clock.advance(pkt.device_ts, pkt.host_ts)
        if event == RESET:
            self.reset()
        t = t_us / 1e6
        if self._last_t is not None and t <= self._last_t:
            self.dropped += 1  # late or repeated: keep the window in time order
            return self._estimate
        self._last_t = t
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
           debounce_s: float = DEBOUNCE_S, breathing: bool = False) -> Iterator[Reading]:
    """Yield a debounced Reading per packet once a full motion window is available."""
    return detect_packets(parse_stream(stream, parser), threshold, debounce_s, breathing)


def detect_packets(packets: Iterable[CSIPacket], threshold: float,
                   debounce_s: float = DEBOUNCE_S, breathing: bool = False) -> Iterator[Reading]:
    """``detect`` for packets already parsed (e.g. loaded from a recording).

    ``breathing=False`` (default) never computes breathing and never emits STILL_PRESENT.
    """
    scorer, debounce = MotionScorer(), _Debouncer(debounce_s)
    breath = _BreathingTracker() if breathing else None
    for pkt in packets:
        score = scorer.push(pkt)
        est = NOT_ENOUGH
        if breath is not None:
            if score is not None and score > threshold:
                breath.reset()
            est = breath.push(pkt)
        if score is not None:
            raw = classify(score, est.bpm, threshold)
            yield Reading(pkt.host_ts, score, debounce.update(raw, pkt.host_ts),
                          est.bpm, est.confidence, est.enough_data)


MIN_CLEAN_S = 8.0                 # clean (kept) empty-room data needed before a threshold is trusted
MIN_CLEAN_FRACTION = 0.5          # and at least this fraction of the clip time must be quiet (else: bursty/busy)
MAX_CALIBRATION_LOSS = 0.6        # refuse only when most packets are missing
MIN_CALIBRATION_PKT_RATE_HZ = 15.0  # below this the window scores are too thin to be stable
CHUNK_S = 1.0                     # contamination is judged per 1 s chunk (see calibrate_clips)
REJECT_RATIO = 3.0                # chunk median > 3x the quiet baseline -> someone is moving
BURST_RATIO = 2.5                 # chunk p90 > 2.5x its own median -> a passer-by inside the chunk
THRESHOLD_PERCENTILE = 99.0
NOISY_PERCENTILE = 97.0           # noisy-room calibration: p97 x margin over all windows, movement included


def packet_loss(device_ts_us: list[int], send_rate_hz: Optional[float] = None) -> float:
    """Fraction of packets missing: 1 - received / expected, with expected from the send rate.

    ``send_rate_hz`` is the sender's nominal rate when known; otherwise it is estimated from timestamps.
    """
    rate = send_rate_hz if send_rate_hz is not None else estimate_rate_hz(device_ts_us)
    duration_s = (device_ts_us[-1] - device_ts_us[0]) / 1e6
    expected = duration_s * rate + 1
    return max(0.0, 1.0 - len(device_ts_us) / expected)


def calibrate(stream: Stream, parser: Optional[CSIParser] = None,
              send_rate_hz: Optional[float] = None, margin: float = DEFAULT_MARGIN) -> Calibration:
    """Learn the motion threshold from an empty-room stream.

    Raises CalibrationError if the input is too short, too sparse, too lossy or too contaminated, and
    ValueError if ``margin`` is not positive.
    """
    return calibrate_packets(list(parse_stream(stream, parser)), send_rate_hz, margin)


def _since_last_reboot(packets: list[CSIPacket]) -> tuple[list[CSIPacket], list[int]]:
    """Packets after the last receiver reboot, with unwrapped device times (us).

    A reboot inside the calibration input restarts the clock, so only the segment
    after it can be measured. Counter wraps are unwrapped and stay in the segment.
    """
    clock, times, start = DeviceClock(), [], 0
    for i, p in enumerate(packets):
        t, event = clock.advance(p.device_ts, p.host_ts)
        if event == RESET:
            start = i
        times.append(t)
    return packets[start:], times[start:]


def calibrate_packets(packets: list[CSIPacket], send_rate_hz: Optional[float] = None,
                      margin: float = DEFAULT_MARGIN) -> Calibration:
    """``calibrate`` for packets already parsed (e.g. loaded from a recording)."""
    return calibrate_clips([packets], send_rate_hz, margin)


def calibrate_clips(clips: Sequence[Sequence[CSIPacket]], send_rate_hz: Optional[float] = None,
                    margin: float = DEFAULT_MARGIN, min_clean_fraction: float = MIN_CLEAN_FRACTION,
                    noisy: bool = False) -> Calibration:
    """Learn one threshold from one or more empty-room clips.

    Each clip is scored on its own (fresh MotionScorer, so no window spans two clips). Scores are
    judged in 1 s chunks: a chunk is contaminated when its median is more than REJECT_RATIO times
    the quiet baseline (the median of the lowest-half chunk medians, pooled over all clips), or when
    its p90 is more than BURST_RATIO times its own median (a passer-by in part of the chunk). The
    threshold is ``margin`` x the THRESHOLD_PERCENTILE of the windows in the kept chunks only.
    Raises CalibrationError if fewer than MIN_CLEAN_S seconds are kept, or if less than
    ``min_clean_fraction`` of the clip time is kept (a bursty or busy link: its quiet leftovers would make a
    threshold that alarms all the time), and ValueError if ``margin`` is not positive.
    """
    margin = validate_margin(margin)
    if not 0 < min_clean_fraction <= 1:
        raise ValueError(f"min_clean_fraction must be in (0, 1], got {min_clean_fraction!r}")
    clips = [list(c) for c in clips]
    if not clips:
        raise CalibrationError("no calibration input")
    chunks: list[tuple[float, np.ndarray]] = []  # (chunk length s, window scores)
    rates, total_s, packets_n, expected_n = [], 0.0, 0, 0.0
    for i, clip in enumerate(clips):
        where = f"clip {i + 1}: " if len(clips) > 1 else ""
        packets, device_ts = _since_last_reboot(clip)
        if len(packets) < 2:
            raise CalibrationError(f"{where}input too short: fewer than two packets")
        duration = (device_ts[-1] - device_ts[0]) / 1e6
        if duration <= 0:
            raise CalibrationError(f"{where}input too short: no time elapsed between packets")
        try:
            rate = send_rate_hz if send_rate_hz is not None else estimate_rate_hz(device_ts)
        except ValueError as e:
            raise CalibrationError(f"{where}input too short: {e}") from e
        rate_eff = len(packets) / duration
        loss = max(0.0, 1.0 - len(packets) / (duration * rate + 1))
        if rate_eff < MIN_CALIBRATION_PKT_RATE_HZ:
            raise CalibrationError(
                f"{where}link too sparse: {rate_eff:.1f} pkt/s < {MIN_CALIBRATION_PKT_RATE_HZ:.0f} pkt/s "
                f"(loss {loss:.0%} of {rate:.0f} Hz); the window scores would be unstable. "
                "Move the nodes closer or check the sender.")
        if loss > MAX_CALIBRATION_LOSS:
            raise CalibrationError(f"{where}input too lossy: {loss:.0%} loss > {MAX_CALIBRATION_LOSS:.0%}")
        rates.append(rate)
        total_s += duration
        packets_n += len(packets)
        expected_n += duration * rate + 1
        per_chunk: dict[int, list[float]] = {}
        scorer = MotionScorer()
        t0 = device_ts[0]
        for p, t in zip(packets, device_ts):
            s = scorer.push(p)
            if s is not None:
                per_chunk.setdefault(int((t - t0) / 1e6 // CHUNK_S), []).append(s)
        for k, scores in per_chunk.items():
            chunks.append((max(0.0, min(CHUNK_S, duration - k * CHUNK_S)), np.array(scores)))
    if not chunks:
        raise CalibrationError("input too short: no full 1 s window")

    medians = np.array([float(np.median(s)) for _, s in chunks])
    order = np.sort(medians)
    baseline = float(np.median(order[: max(1, len(order) // 2)]))
    kept: list[np.ndarray] = []
    clean_s = rejected_s = 0.0
    for (length, scores), median in zip(chunks, medians):
        bursty = float(np.percentile(scores, 90)) > BURST_RATIO * median
        if median > REJECT_RATIO * baseline or bursty:
            rejected_s += length
        else:
            kept.append(scores)
            clean_s += length
    warning = ""
    if noisy:
        # A noisy room: movement is not refused. Every window counts, and the threshold is p97 x margin over
        # all of them (the stopgap that worked on the real rig). The warning says how much looked like movement.
        kept = [scores for _, scores in chunks]
        clean_s = sum(length for length, _ in chunks)
        percentile = NOISY_PERCENTILE
        if rejected_s > 0:  # a clean clip carries no warning
            warning = (f"noisy: {rejected_s:.1f} of {total_s:.1f} s looked like movement; threshold = "
                       f"p{NOISY_PERCENTILE:g} x margin over all windows")
    else:
        if clean_s < MIN_CLEAN_S:
            raise CalibrationError(
                f"input too short: {clean_s:.1f} s of clean data < {MIN_CLEAN_S:.0f} s "
                f"(rejected {rejected_s:.1f} s of {total_s:.1f} s as contaminated by movement). "
                "Record the empty room again, with nobody walking through.")
        if clean_s < min_clean_fraction * total_s:
            raise CalibrationError(
                f"only {clean_s:.1f} s of {total_s:.1f} s was quiet; the link is bursty or someone was moving: "
                "record again")
        percentile = THRESHOLD_PERCENTILE
    pooled = np.concatenate(kept)
    threshold = float(np.percentile(pooled, percentile)) * margin
    loss = max(0.0, 1.0 - packets_n / expected_n)
    return Calibration(threshold=threshold,
                       rate_hz=float(send_rate_hz) if send_rate_hz is not None else float(np.median(rates)),
                       duration_s=total_s, loss=loss, clean_s=clean_s, rejected_s=rejected_s,
                       pkt_rate_hz=packets_n / total_s, clips=len(clips), margin=margin,
                       noisy=noisy, warning=warning)
