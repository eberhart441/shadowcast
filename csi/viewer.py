"""Data prep for the live viewer: rolling amplitude and motion-score buffers.

Pure data, no plotting or I/O. Consumes the (host_ts, line) stream seam and
reuses the parser (csi.parser), link health (csi.health) and motion scorer
(csi.detect) rather than reimplementing them.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from .detect import SIM_THRESHOLD, MotionScorer
from .health import LinkHealth
from .parser import CSIParser, Stream

HISTORY_S = 10.0          # seconds shown in the heatmap and variance trace
THRESHOLD_STEP = 1.25     # multiplicative change per key press


class LiveView:
    """Rolling buffers for the viewer, fed one (host_ts, line) at a time.

    ``threshold`` defaults to the simulator's score scale; real ports must pass one explicitly
    (the CLI enforces that via ``csi.common.resolve_threshold``).
    """

    def __init__(self, threshold: float = SIM_THRESHOLD, send_rate_hz: float = 100.0,
                 history_s: float = HISTORY_S, parser: Optional[CSIParser] = None) -> None:
        self.parser = parser or CSIParser()
        self.health = LinkHealth(send_rate_hz=send_rate_hz)
        self._scorer = MotionScorer()
        self._history_s = history_s
        self._amps: deque[tuple[float, np.ndarray]] = deque()
        self._scores: deque[tuple[float, float]] = deque()
        self._latest: Optional[float] = None
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    def adjust_threshold(self, steps: int) -> None:
        """Move the threshold by ``steps`` key presses (negative lowers it)."""
        self._threshold *= THRESHOLD_STEP ** steps

    def ingest(self, host_ts: float, line: str) -> None:
        """Parse one line; malformed or non-CSI lines are counted by the parser."""
        pkt = self.parser.parse_line(line, host_ts)
        if pkt is None:
            return
        self.health.observe(pkt)
        self._latest = host_ts
        self._amps.append((host_ts, pkt.amplitude))
        score = self._scorer.push(pkt)
        if score is not None:
            self._scores.append((host_ts, score))
        self._prune(host_ts)

    def consume(self, stream: Stream) -> None:
        for host_ts, line in stream:
            self.ingest(host_ts, line)

    def heatmap(self) -> np.ndarray:
        """Amplitudes as a (subcarriers, packets) matrix, oldest column first."""
        if not self._amps:
            return np.zeros((0, 0), dtype=np.float32)
        return np.stack([a for _, a in self._amps], axis=1)

    def heatmap_times(self) -> np.ndarray:
        """Seconds relative to the latest packet, one per heatmap column."""
        if not self._amps or self._latest is None:
            return np.zeros(0)
        return np.asarray([t for t, _ in self._amps]) - self._latest

    def variance_trace(self) -> tuple[np.ndarray, np.ndarray]:
        """(seconds relative to the latest packet, motion score) per scored packet."""
        if not self._scores or self._latest is None:
            return np.zeros(0), np.zeros(0)
        ts, scores = zip(*self._scores)
        return np.asarray(ts) - self._latest, np.asarray(scores)

    def title(self) -> str:
        return (f"{self.health.packet_rate():.1f} pkt/s  |  drops {self.health.drops}  |  "
                f"malformed {self.parser.stats.malformed}  |  threshold {self._threshold:.2f}")

    def _prune(self, now: float) -> None:
        horizon = now - self._history_s
        while self._amps and self._amps[0][0] < horizon:
            self._amps.popleft()
        while self._scores and self._scores[0][0] < horizon:
            self._scores.popleft()
