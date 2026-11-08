"""Adaptive motion detection: each link learns its own noise level, and a room decision needs link agreement.

A fixed threshold goes stale when the room is never quiet or a receiver re-baselines its gain (its scores
then move by 3-5x). The adaptive mode needs no calibration. Per link, ``AdaptiveDetector`` keeps a rolling
baseline of its own score over the last ``window_s`` of packet time:

    baseline  median of the kept samples
    spread    1.4826 x median absolute deviation (a robust sigma)
    enter     max(baseline + k x spread, ratio_floor x baseline)
    exit      max(baseline + exit_k x spread, exit_ratio x baseline)     (lower than enter: hysteresis)

A link is ``elevated`` (state MOTION) once its score has stayed at or above ``enter`` for ``min_dur_s``, and it
leaves when the score drops below ``exit``. The first ``warmup_s`` of packet time (and at least
``min_samples`` samples) report WARMUP and never alarm. The baseline is a median, so a person moving through
(10-30 s against a 75 s window) only moves it part of the way; ``EXCLUDE_HIGH`` (off) would keep high samples out
of it, which biased the median low on drifting noise. If most of the last ``cap_s`` (``rebuild_fraction`` of the
samples) sat at or above ``enter``, or at or below ``baseline / ratio_floor``, that level is taken as the new
normal: the baseline is rebuilt from the samples of the last ``cap_s`` (a receiver re-baseline, or a room that
stays busy). So a shift is relearned within about ``cap_s``, and a person who stands still for that long is absorbed.

``RoomDecision`` combines the links: MOTION when at least ``min_links`` links are elevated at once, or one link
is ``strong`` (at least ``strong_ratio`` x its enter level, sustained). Otherwise EMPTY, held for ``hold_s``.

Both classes take (time, score) series, so they can be driven by packets (LinkMonitor) or by a recorded
series (``replay_series``). Time is packet time in seconds, never the wall clock.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np

from .detect import _Debouncer

EMPTY = "EMPTY"
MOTION = "MOTION"
WARMUP = "WARMUP"

WINDOW_S = 75.0
WARMUP_S = 10.0
MIN_SAMPLES = 20
K = 5.5                 # enter at baseline + K robust sigmas ...
RATIO_FLOOR = 2.0       # ... and at least RATIO_FLOOR x baseline
EXIT_K = 2.5
EXIT_RATIO = 1.5
MIN_DUR_S = 0.6         # sustained at or above enter for this long before elevated
CAP_S = 30.0            # a level that fills most of this long is taken as the new normal
REBUILD_FRACTION = 0.8  # ... where "most" = this share of samples high (or low) over the cap window
EXCLUDE_HIGH = False    # keep high samples in the baseline too: the 75 s median already resists a 10-30 s walk, and
                        # excluding them biased the median low on drifting noise (real captures; see the report)
RECALC_S = 0.25         # baseline statistics are refreshed at most this often (packet time)
STRONG_RATIO = 2.0      # strong: at least this x the enter level, sustained
ROOM_MIN_LINKS = 2
ROOM_HOLD_S = 1.0
MAD_SCALE = 1.4826
_TINY = 1e-9


@dataclass(frozen=True)
class Reading:
    t: float
    state: str                  # WARMUP, EMPTY or MOTION (= elevated)
    score: Optional[float]
    elevated: bool = False
    strong: bool = False
    level: Optional[float] = None      # score / enter; >= 1 at or above the enter level
    baseline: Optional[float] = None
    spread: Optional[float] = None
    enter: Optional[float] = None
    exit: Optional[float] = None
    warm: bool = False


class AdaptiveDetector:
    def __init__(self, *, window_s: float = WINDOW_S, warmup_s: float = WARMUP_S,
                 min_samples: int = MIN_SAMPLES, k: float = K, ratio_floor: float = RATIO_FLOOR,
                 exit_k: float = EXIT_K, exit_ratio: float = EXIT_RATIO, min_dur_s: float = MIN_DUR_S,
                 cap_s: float = CAP_S, rebuild_fraction: float = REBUILD_FRACTION, exclude_high: bool = EXCLUDE_HIGH,
                 recalc_s: float = RECALC_S, strong_ratio: float = STRONG_RATIO) -> None:
        self.window_s, self.warmup_s, self.min_samples = window_s, warmup_s, min_samples
        self.exclude_high = exclude_high
        self.k, self.ratio_floor, self.exit_k, self.exit_ratio = k, ratio_floor, exit_k, exit_ratio
        self.min_dur_s, self.cap_s, self.recalc_s, self.strong_ratio = min_dur_s, cap_s, recalc_s, strong_ratio
        self.rebuild_fraction = rebuild_fraction
        self._all: deque[tuple[float, float]] = deque()   # every sample in the window (for rebuilds)
        self._base: deque[tuple[float, float]] = deque()  # the samples the baseline is computed from
        self._first_t: Optional[float] = None
        self._last_t: Optional[float] = None
        self._stats: Optional[tuple[float, float, float, float]] = None  # baseline, spread, enter, exit
        self._stats_t: Optional[float] = None
        self._elevated = False
        self._enter_since: Optional[float] = None
        self._strong_since: Optional[float] = None
        self._hist: deque[tuple[float, bool, bool]] = deque()  # (t, high, low) over the last cap_s
        self._last: Optional[Reading] = None

    # ------------------------------------------------------------------ public

    def push(self, t: float, score: Optional[float]) -> Reading:
        """One (time, score) sample. ``None`` (no score yet) and out-of-order times change nothing."""
        if score is None or (self._last_t is not None and t <= self._last_t):
            return self._last if self._last is not None else Reading(t, WARMUP, score)
        self._last_t = t
        if self._first_t is None:
            self._first_t = t
        self._all.append((t, score))
        self._trim(t)
        if not self._is_warm(t):
            self._base.append((t, score))
            self._stats = None
            return self._set(Reading(t, WARMUP, score))
        self._refresh(t)
        baseline, spread, enter, exit_ = self._stats
        high = score >= enter
        low = score <= baseline / self.ratio_floor
        if not high or not self.exclude_high:  # high samples stay out of the baseline until a sustained high level is taken as the new normal
            self._base.append((t, score))
        self._hist.append((t, high, low))
        while self._hist and self._hist[0][0] < t - self.cap_s:
            self._hist.popleft()
        if self._should_rebuild(t):
            self._rebuild(t - self.cap_s)
        if self._stats is None:  # rebuilt: the thresholds follow the new baseline
            self._refresh(t)
            baseline, spread, enter, exit_ = self._stats
        self._update_elevation(t, score, enter, exit_)
        strong = self._update_strong(t, score, enter)
        state = MOTION if self._elevated else EMPTY
        return self._set(Reading(t, state, score, elevated=self._elevated, strong=strong,
                                 level=score / enter if enter > 0 else None, baseline=baseline,
                                 spread=spread, enter=enter, exit=exit_, warm=True))

    # ------------------------------------------------------------------ internals

    def _set(self, reading: Reading) -> Reading:
        self._last = reading
        return reading

    def _trim(self, t: float) -> None:
        while self._all and self._all[0][0] < t - self.window_s:
            self._all.popleft()
        while self._base and self._base[0][0] < t - self.window_s:
            self._base.popleft()

    def _is_warm(self, t: float) -> bool:
        return t - self._first_t >= self.warmup_s and len(self._base) >= self.min_samples

    def _refresh(self, t: float) -> None:
        if self._stats is not None and self._stats_t is not None and t - self._stats_t < self.recalc_s:
            return
        values = np.array([s for _, s in self._base])
        baseline = max(float(np.median(values)), _TINY)
        spread = MAD_SCALE * float(np.median(np.abs(values - baseline)))
        enter = max(baseline + self.k * spread, self.ratio_floor * baseline)
        exit_ = max(baseline + self.exit_k * spread, self.exit_ratio * baseline)
        self._stats = (baseline, spread, enter, exit_)
        self._stats_t = t

    def _should_rebuild(self, t: float) -> bool:
        """Most of the last ``cap_s`` sat above enter (or below baseline / ratio_floor): the level is the new normal.

        A fraction rather than an unbroken run, because a real shift in a noisy link still has quiet samples in it.
        """
        if not self._hist or self._hist[0][0] > t - 0.9 * self.cap_s:
            return False
        n = len(self._hist)
        high = sum(h for _, h, _ in self._hist) / n
        low = sum(lo for _, _, lo in self._hist) / n
        return high >= self.rebuild_fraction or low >= self.rebuild_fraction

    def _rebuild(self, since: float) -> None:
        """The level since ``since`` is the new normal: the baseline is made of those samples only."""
        self._base = deque((t, s) for t, s in self._all if t >= since)
        self._hist.clear()
        self._stats = None
        self._stats_t = None

    def _update_elevation(self, t: float, score: float, enter: float, exit_: float) -> None:
        if not self._elevated:
            if score >= enter:
                if self._enter_since is None:
                    self._enter_since = t
                if t - self._enter_since >= self.min_dur_s:
                    self._elevated = True
            elif score < exit_:
                self._enter_since = None
        elif score < exit_:
            self._elevated = False
            self._enter_since = None

    def _update_strong(self, t: float, score: float, enter: float) -> bool:
        if self._elevated and score >= self.strong_ratio * enter:
            if self._strong_since is None:
                self._strong_since = t
        else:
            self._strong_since = None
        return self._strong_since is not None and t - self._strong_since >= self.min_dur_s


class RoomDecision:
    """Room MOTION from link agreement (see the module doc). Feed it the readings of the live links."""

    def __init__(self, *, min_links: int = ROOM_MIN_LINKS, strong_ratio: float = STRONG_RATIO,
                 hold_s: float = ROOM_HOLD_S) -> None:
        self.min_links = min_links
        self._debounce = _Debouncer(hold_s)
        self.state = WARMUP
        self.elevated: list[str] = []

    def update(self, t: float, readings: Mapping[str, Reading]) -> str:
        """``readings`` maps link label to its current Reading; links with no recent packet are left out."""
        warm = [r for r in readings.values() if r.warm]
        self.elevated = sorted(label for label, r in readings.items() if r.elevated)
        strong = any(r.strong for r in readings.values())
        if not warm:
            self.state = WARMUP
            return self.state
        raw = MOTION if (len(self.elevated) >= self.min_links or strong) else EMPTY
        self.state = self._debounce.update(raw, t)
        return self.state


def replay_series(series: Mapping[str, Sequence[tuple[float, float]]], *, room: bool = True,
                  **params) -> dict:
    """Drive one detector per link from (time, score) series, merged in time order.

    Returns ``{"links": {label: [Reading, ...]}, "room": [(t, state), ...]}``; the lists follow the merged
    time order. ``params`` go to ``AdaptiveDetector``. Links with no sample yet are not in the room decision.
    """
    dets = {label: AdaptiveDetector(**params) for label in series}
    decision = RoomDecision()
    merged = sorted(((t, label, s) for label, pts in series.items() for t, s in pts), key=lambda x: (x[0], x[1]))
    out_links: dict[str, list[Reading]] = {label: [] for label in series}
    out_room: list[tuple[float, str]] = []
    current: dict[str, Reading] = {}
    for t, label, score in merged:
        reading = dets[label].push(t, score)
        out_links[label].append(reading)
        current[label] = reading
        if room:
            out_room.append((t, decision.update(t, current)))
    return {"links": out_links, "room": out_room}
