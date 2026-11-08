"""Adaptive motion detection: each link learns its own noise level online, and a room decision needs link agreement.

A fixed threshold needs an empty-room calibration, and it goes stale when a receiver re-baselines its gain (its
scores then move by 3-5x). The adaptive mode needs neither. Per link, ``AdaptiveDetector`` keeps a rolling window
of its own score (one sample per ``sample_s``) over the last ``window_s`` (180 s) of packet time and sets a
constant-false-alarm level from the lower ``base_fraction`` (60%) of it, which movement cannot reach (movement
only adds to the top):

    baseline  median of the lower 60% of the window's scores
    spread    1.4826 x their median absolute deviation from it (a robust sigma of the quiet noise)
    enter     max(baseline + k x spread, ratio_floor x baseline)     k = 4, ratio floor 1.5
    exit      max(baseline + exit_k x spread, exit_ratio x baseline) (lower than enter: hysteresis)

A link is ``elevated`` (state MOTION) once its score has stayed at or above ``enter`` for ``min_dur_s``, and it
leaves when the score drops below ``exit``. The first ``warmup_s`` of packet time (and at least ``min_samples``
samples) report WARMUP and never alarm. While a link is elevated its baseline is frozen: no samples go in and none
age out, so a person who keeps moving stays detected instead of being learned as the new normal. A level that
lasts ``freeze_cap_s`` (300 s) is taken as the new normal: the baseline is rebuilt from the last ``cap_s`` of
samples. A level that drops (``rebuild_fraction`` of the last ``cap_s`` at or below ``baseline / ratio_floor``) is
relearned the same way. A missing score after valid ones (``None``: the scorer restarted its window, after a
receiver reboot, which re-rolls the receiver's gain, or a gap of about a window) starts the detector over.

With the 2 s motion window this enters at about 1.5-1.8x the quiet level, so the 1.8-3x lifts a walker gave on
the real rig are caught. On the recorded hw3/hw5 links (25 pkt/s, enter estimated from their empty recordings)
every walking window was above enter, against 0% (hw5) and 5% (hw3, gain drifting) of the empty windows. Bursts
in an "empty" room are not told apart from people: one hw5 empty recording has a 3x burst, which reads MOTION.

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

WINDOW_S = 180.0        # baseline window, in packet time spent not frozen
WARMUP_S = 10.0
MIN_SAMPLES = 20
BASE_FRACTION = 0.6     # the baseline and spread come from the lowest 60% of the window's scores
K = 4.0                 # enter at baseline + K robust sigmas ...
RATIO_FLOOR = 1.5       # ... and at least RATIO_FLOOR x baseline
EXIT_K = 2.5
EXIT_RATIO = 1.25
MIN_DUR_S = 0.6         # sustained at or above enter for this long before elevated
CAP_S = 30.0            # a lower level that fills most of this long is taken as the new normal
REBUILD_FRACTION = 0.8  # ... where "most" = this share of the samples over the cap window
FREEZE_CAP_S = 300.0    # the baseline stays frozen while a link is elevated, for at most this long
SAMPLE_S = 0.1          # one score per this much packet time goes into the windows (each score spans 2 s)
EXCLUDE_HIGH = False    # samples at or above enter (before the link is elevated) stay in the window: the
                        # lower-60% statistics ignore them anyway
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
                 recalc_s: float = RECALC_S, strong_ratio: float = STRONG_RATIO,
                 base_fraction: float = BASE_FRACTION, freeze_cap_s: float = FREEZE_CAP_S,
                 sample_s: float = SAMPLE_S) -> None:
        self.window_s, self.warmup_s, self.min_samples = window_s, warmup_s, min_samples
        self.exclude_high = exclude_high
        self.k, self.ratio_floor, self.exit_k, self.exit_ratio = k, ratio_floor, exit_k, exit_ratio
        self.min_dur_s, self.cap_s, self.recalc_s, self.strong_ratio = min_dur_s, cap_s, recalc_s, strong_ratio
        self.rebuild_fraction, self.base_fraction = rebuild_fraction, base_fraction
        self.freeze_cap_s, self.sample_s = freeze_cap_s, sample_s
        self._last: Optional[Reading] = None
        self._start()

    def _start(self) -> None:
        """Forget everything learned: a new link, or one whose scorer restarted."""
        self._recent: deque[tuple[float, float]] = deque()  # (t, score) samples of the last cap_s, for rebuilds
        self._base: deque[tuple[float, float]] = deque()    # (baseline clock, score): what the statistics use
        self._low: deque[tuple[float, bool]] = deque()      # (t, score low) over the last cap_s
        self._low_count = 0
        self._first_t: Optional[float] = None
        self._last_t: Optional[float] = None
        self._sample_t: Optional[float] = None  # packet time of the last sample put in the windows
        self._frozen_s = 0.0                    # packet time spent elevated; the baseline clock is t - frozen_s
        self._stats: Optional[tuple[float, float, float, float]] = None  # baseline, spread, enter, exit
        self._stats_t: Optional[float] = None
        self._elevated = False
        self._elevated_since: Optional[float] = None
        self._enter_since: Optional[float] = None
        self._strong_since: Optional[float] = None

    # ------------------------------------------------------------------ public

    def push(self, t: float, score: Optional[float]) -> Reading:
        """One (time, score) sample. Out-of-order times change nothing. ``None`` (no score) changes nothing before
        the first valid score; after one it means the scorer restarted its window, so the detector starts over."""
        if score is None:
            if self._first_t is not None:
                self._start()
                return self._set(Reading(t, WARMUP, None))
            return self._last if self._last is not None else Reading(t, WARMUP, score)
        if self._last_t is not None and t <= self._last_t:
            return self._last if self._last is not None else Reading(t, WARMUP, score)
        if self._elevated:
            self._frozen_s += t - self._last_t  # frozen: the baseline clock stands still
        self._last_t = t
        if self._first_t is None:
            self._first_t = t
        sampled = self._sample_t is None or t - self._sample_t >= self.sample_s
        if sampled:
            self._sample_t = t
            self._recent.append((t, score))
            while self._recent[0][0] < t - self.cap_s:
                self._recent.popleft()
        if not self._is_warm(t):
            if sampled:
                self._add_base(t, score)
            self._stats = None
            return self._set(Reading(t, WARMUP, score))
        self._refresh(t)
        baseline, spread, enter, exit_ = self._stats
        if sampled and not self._elevated and (score < enter or not self.exclude_high):
            self._add_base(t, score)
        self._track_low(t, score <= baseline / self.ratio_floor)
        if self._low_level_lasted(t) or self._freeze_expired(t):
            self._rebuild(t, score)
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

    def _add_base(self, t: float, score: float) -> None:
        clock = t - self._frozen_s
        self._base.append((clock, score))
        while self._base[0][0] < clock - self.window_s:
            self._base.popleft()

    def _is_warm(self, t: float) -> bool:
        return t - self._first_t >= self.warmup_s and len(self._base) >= self.min_samples

    def _refresh(self, t: float) -> None:
        """Baseline statistics from the lower ``base_fraction`` of the window. Unchanged while frozen."""
        if self._stats is not None and (self._elevated or t - self._stats_t < self.recalc_s):
            return
        values = np.fromiter((s for _, s in self._base), dtype=np.float64, count=len(self._base))
        n_low = max(1, int(round(self.base_fraction * values.size)))
        low = np.partition(values, n_low - 1)[:n_low]
        baseline = max(float(np.median(low)), _TINY)
        spread = MAD_SCALE * float(np.median(np.abs(low - baseline)))
        enter = max(baseline + self.k * spread, self.ratio_floor * baseline)
        exit_ = max(baseline + self.exit_k * spread, self.exit_ratio * baseline)
        self._stats = (baseline, spread, enter, exit_)
        self._stats_t = t

    def _track_low(self, t: float, low: bool) -> None:
        self._low.append((t, low))
        self._low_count += low
        while self._low[0][0] < t - self.cap_s:
            self._low_count -= self._low.popleft()[1]

    def _low_level_lasted(self, t: float) -> bool:
        """Most of the last ``cap_s`` sat at or below baseline / ratio_floor: the lower level is the new normal.

        A fraction rather than an unbroken run, because a real shift in a noisy link still has other samples in it.
        """
        if self._low[0][0] > t - 0.9 * self.cap_s:
            return False
        return self._low_count >= self.rebuild_fraction * len(self._low)

    def _freeze_expired(self, t: float) -> bool:
        return self._elevated and t - self._elevated_since >= self.freeze_cap_s

    def _rebuild(self, t: float, score: float) -> None:
        """The level of the last ``cap_s`` is the new normal: the baseline is made of those samples only."""
        clock = t - self._frozen_s
        recent = [(ts, s) for ts, s in self._recent if ts >= t - self.cap_s] or [(t, score)]
        self._base = deque((clock - (t - ts), s) for ts, s in recent)
        self._low.clear()
        self._low_count = 0
        self._stats = None
        self._stats_t = None
        if self._elevated:
            self._elevated_since = t  # if the link stays elevated, the freeze cap counts from the new normal

    def _update_elevation(self, t: float, score: float, enter: float, exit_: float) -> None:
        if not self._elevated:
            if score >= enter:
                if self._enter_since is None:
                    self._enter_since = t
                if t - self._enter_since >= self.min_dur_s:
                    self._elevated, self._elevated_since = True, t
            elif score < exit_:
                self._enter_since = None
        elif score < exit_:
            self._elevated, self._elevated_since = False, None
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
