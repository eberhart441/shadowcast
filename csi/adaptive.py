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

``RoomDecision`` combines the links. N is the number of live links (warm, and heard within the NO_SIGNAL window;
WARMUP, UNCALIBRATED and silent links are left out). The room enters MOTION once all of these have held for
``enter_s`` (0.5 s), and leaves it ``hold_s`` (1 s) after they stop (agreement that returns within the hold needs
no new ``enter_s``):

    count      at least k = max(min_links, ceil(fraction x N)) links are elevated: 2 up to 13 links, 3 for 14-20,
               5 for 30. A fixed k = 2 lets independent per-link false alarms add up: at 2% per link per 2 s
               window, P(at least 2 of 16) is 4% (about 70 alarms an hour); P(at least 3 of 16) is 0.37%, and
               P(at least 5 of 30) 0.03%, before the tests below.
    diversity  they come from at least ``min_receivers`` (2) receivers and ``min_senders`` (2) senders, each capped
               at what is live: one node's glitch is not a person.
    place      with the room's geometry (``places`` with segments), at least k of them pass within ``spatial_m``
               (1 m) of one point, and those links are diverse too, so links that only meet at a shared node do
               not agree. A person disturbs links that cross near them; noise lights links anywhere. The point
               is searched on a grid (``grid_m``) and reported as ``support``: {x, y, links, radius_m}.
    glitch     a node with at least ``glitch_min_links`` (3) live links, ``glitch_share`` (70%) of them elevated
               while at most ``glitch_quiet_share`` (30%) of the other links are, is a receiver- or sender-wide
               glitch (gain change, reset, export backlog) or someone right next to that node (the heatmap's
               near-node veto, same shares): its links do not count for that update (``glitch_nodes``).

A rig of at most ``small_rig_links`` (4) live links, today's 2 x 2 rig, keeps the old rule apart from ``enter_s``
and the place test: any two elevated links agree (node diversity would leave only 2 of its 6 pairs), and one link
that is ``strong`` (at least ``strong_ratio`` x its enter level, sustained) is enough on its own. Fixed thresholds
and auto mode share this decision (csi/links.py LinkMonitor). Offline, scripts/eval_log.py's scaling curve still
votes a plain "k of n" (default k = 2, ``--k`` to change) over the logged link states, without the tests above,
and csi/evaluate.py (recorded sessions) takes any link in MOTION; the room row of eval_log.py is the logged
decision.

Both classes take (time, score) series, so they can be driven by packets (LinkMonitor) or by a recorded
series (``replay_series``). Time is packet time in seconds, never the wall clock.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Mapping, NamedTuple, Optional, Sequence

import numpy as np

from .detect import _Debouncer
from .room import point_segment_distance

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
ROOM_MIN_LINKS = 2      # the room needs at least this many elevated links ...
ROOM_FRACTION = 0.15    # ... and at least this share of the live links: k = max(2, ceil(0.15 N))
ROOM_HOLD_S = 1.0       # MOTION is held this long after the links stop agreeing
ROOM_ENTER_S = 0.5      # the links must agree this long before the room enters MOTION
ROOM_SMALL_RIG_LINKS = 4  # up to this many live links: any two links agree, and one strong link is enough
ROOM_MIN_RECEIVERS = 2  # bigger rigs: the agreeing links come from this many receivers ...
ROOM_MIN_SENDERS = 2    # ... and senders (each capped at what is live)
ROOM_SPATIAL_M = 1.0    # with the room's geometry: k agreeing links pass within this distance of one point
ROOM_GRID_M = 0.25      # spacing of the points searched for it ...
ROOM_GRID_MAX_POINTS = 4000  # ... coarser for a large area, so the search stays cheap
ROOM_GLITCH_SHARE = 0.7        # a node with this share of its live links elevated ... (csi/heatmap.py BUSY_SHARE)
ROOM_GLITCH_QUIET_SHARE = 0.3  # ... while at most this share of the other links are (QUIET_SHARE) ...
ROOM_GLITCH_MIN_LINKS = 3      # ... and at least this many live links (BUSY_MIN_LINKS) is a glitch: set aside
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


def required_links(n_active: int, min_links: int = ROOM_MIN_LINKS, fraction: float = ROOM_FRACTION) -> int:
    """Elevated links the room needs with ``n_active`` live links: ``max(min_links, ceil(fraction x n_active))``.

    The epsilon keeps a product that is a whole number (0.15 x 20 = 3) from rounding up to the next one."""
    return max(min_links, math.ceil(fraction * n_active - 1e-9))


class LinkPlace(NamedTuple):
    """A link's receiver and sender (node names) and, when the room is known, its segment: ((x, y), (x, y)) m."""
    receiver: str
    sender: str
    segment: Optional[tuple[tuple[float, float], tuple[float, float]]] = None


def label_place(label: str) -> LinkPlace:
    """A link's nodes from its label: ``rx1<-01`` is receiver ``rx1``, sender ``01`` (no segment). A label without
    ``<-`` is its own receiver and sender, so it shares a node with no other link."""
    receiver, sep, sender = label.partition("<-")
    return LinkPlace(receiver, sender) if sep else LinkPlace(label, label)


class RoomDecision:
    """Room MOTION from link agreement (see the module doc). Feed it the readings of the live links.

    ``places`` maps a link label to its LinkPlace, or to a (receiver, sender[, segment]) tuple; a link without one
    takes its nodes from its label (``label_place``) and has no segment. ``bounds`` (x0, y0, x1, y1, metres) is the
    area searched for the common point, by default the box round the segments. No segment, no place test; a link
    with no segment among ones that have one could be anywhere, so it counts at every point.

    After each ``update``: ``state``; ``elevated`` (every elevated link); ``counted`` (the elevated links that count,
    not of a glitching node); ``active`` (N); ``required`` (k); ``support`` (while MOTION from the place test:
    {x, y, links, radius_m}, else None); ``glitch_nodes``; and ``reason``, one of warmup, quiet, too_few, diversity,
    spatial, entering (agreeing, not yet for ``enter_s``), agree, strong, hold.
    """

    def __init__(self, *, min_links: int = ROOM_MIN_LINKS, strong_ratio: float = STRONG_RATIO,
                 hold_s: float = ROOM_HOLD_S, fraction: float = ROOM_FRACTION, enter_s: float = ROOM_ENTER_S,
                 small_rig_links: int = ROOM_SMALL_RIG_LINKS, min_receivers: int = ROOM_MIN_RECEIVERS,
                 min_senders: int = ROOM_MIN_SENDERS, spatial_m: Optional[float] = ROOM_SPATIAL_M,
                 glitch_share: float = ROOM_GLITCH_SHARE, glitch_quiet_share: float = ROOM_GLITCH_QUIET_SHARE,
                 glitch_min_links: int = ROOM_GLITCH_MIN_LINKS, places: Optional[Mapping[str, Sequence]] = None,
                 bounds: Optional[Sequence[float]] = None, grid_m: float = ROOM_GRID_M) -> None:
        self.min_links, self.fraction, self.enter_s, self.strong_ratio = min_links, fraction, enter_s, strong_ratio
        self.small_rig_links, self.min_receivers, self.min_senders = small_rig_links, min_receivers, min_senders
        self.spatial_m = spatial_m
        self.glitch_share, self.glitch_quiet_share = glitch_share, glitch_quiet_share
        self.glitch_min_links = glitch_min_links
        self._debounce = _Debouncer(hold_s)
        self._places: dict[str, LinkPlace] = {label: LinkPlace(*place) for label, place in (places or {}).items()}
        segments = {label: p.segment for label, p in self._places.items() if p.segment is not None}
        self._grid: Optional[np.ndarray] = None
        self._dist: dict[str, np.ndarray] = {}   # link label -> distance from its segment to each grid point
        if segments and spatial_m:
            self._grid = _grid_points(list(segments.values()), bounds, grid_m)
            self._dist = {label: point_segment_distance(self._grid, a, b) for label, (a, b) in segments.items()}
        self._cache: tuple = (None, None)         # (counted links and needs, their common point) of the last search
        self._ok_since: Optional[float] = None
        self._held: Optional[dict] = None
        self.state = WARMUP
        self.elevated: list[str] = []
        self.counted: list[str] = []
        self.active = 0
        self.required = required_links(0, min_links, fraction)
        self.support: Optional[dict] = None
        self.glitch_nodes: list[str] = []
        self.reason = "warmup"

    def update(self, t: float, readings: Mapping[str, Reading]) -> str:
        """``readings`` maps link label to its current Reading; links with no recent packet are left out."""
        self.elevated = sorted(label for label, r in readings.items() if r.elevated)
        live = [label for label, r in readings.items() if r.warm]
        self.active = len(live)
        self.required = required_links(self.active, self.min_links, self.fraction)
        if not live:
            self._ok_since = self._held = self.support = None
            self.counted, self.glitch_nodes = [], []
            self.state, self.reason = WARMUP, "warmup"
            return self.state
        ok, reason, support = self._agreement(live, readings)
        if ok:
            if self._ok_since is None:
                self._ok_since = t
            entered = self.state == MOTION or t - self._ok_since >= self.enter_s
        else:
            self._ok_since, entered = None, False
        self.state = self._debounce.update(MOTION if entered else EMPTY, t)
        if self.state == MOTION:
            if entered:
                self._held = support
            self.reason = reason if entered else "hold"
        else:
            self._held = None
            self.reason = "entering" if ok else reason
        self.support = self._held
        return self.state

    def _place(self, label: str) -> LinkPlace:
        place = self._places.get(label)
        if place is None:
            place = self._places[label] = label_place(label)
        return place

    def _agreement(self, live: Sequence[str],
                   readings: Mapping[str, Reading]) -> tuple[bool, str, Optional[dict]]:
        """Whether the live links agree on movement now: (ok, reason, support). Sets ``counted`` and
        ``glitch_nodes``."""
        n, k = len(live), self.required
        rx_live: dict[str, int] = {}
        tx_live: dict[str, int] = {}
        rx_up: dict[str, int] = {}
        tx_up: dict[str, int] = {}
        up: list[tuple[str, LinkPlace]] = []
        for label in live:
            place = self._place(label)
            rx_live[place.receiver] = rx_live.get(place.receiver, 0) + 1
            tx_live[place.sender] = tx_live.get(place.sender, 0) + 1
            if readings[label].elevated:
                up.append((label, place))
                rx_up[place.receiver] = rx_up.get(place.receiver, 0) + 1
                tx_up[place.sender] = tx_up.get(place.sender, 0) + 1
        bad_rx = self._glitching(rx_live, rx_up, n, len(up))
        bad_tx = self._glitching(tx_live, tx_up, n, len(up))
        self.glitch_nodes = sorted(bad_rx | bad_tx)
        counted = [(label, p) for label, p in up if p.receiver not in bad_rx and p.sender not in bad_tx]
        self.counted = sorted(label for label, _ in counted)
        small = n <= self.small_rig_links
        if small and any(readings[label].strong for label, _ in counted):
            return True, "strong", None
        if not counted:
            return False, "quiet", None
        if len(counted) < k:
            return False, "too_few", None
        need_rx = 1 if small else min(self.min_receivers, len(rx_live))
        need_tx = 1 if small else min(self.min_senders, len(tx_live))
        if len({p.receiver for _, p in counted}) < need_rx or len({p.sender for _, p in counted}) < need_tx:
            return False, "diversity", None
        if self._grid is None or not any(label in self._dist for label, _ in counted):
            return True, "agree", None
        support = self._common_point(counted, k, need_rx, need_tx)
        return (False, "spatial", None) if support is None else (True, "agree", support)

    def _glitching(self, live: Mapping[str, int], up: Mapping[str, int], n: int, n_up: int) -> set[str]:
        """Nodes whose live links are mostly elevated while the other links are mostly quiet (module doc)."""
        nodes: set[str] = set()
        if not n_up:
            return nodes
        for node, mine in live.items():
            others = n - mine
            if mine < self.glitch_min_links or others <= 0:
                continue
            raised = up.get(node, 0)
            if raised / mine >= self.glitch_share and (n_up - raised) / others <= self.glitch_quiet_share:
                nodes.add(node)
        return nodes

    def _common_point(self, counted: Sequence[tuple[str, LinkPlace]], k: int, need_rx: int,
                      need_tx: int) -> Optional[dict]:
        """The grid point with the most counted links within ``spatial_m``, if at least ``k`` of them are, from at
        least ``need_rx`` receivers and ``need_tx`` senders: {x, y, links, radius_m}. None if no point has that.
        Ties go to the smallest summed distance, so the point sits where the links cross."""
        key = (tuple(sorted(label for label, _ in counted)), k, need_rx, need_tx)
        if self._cache[0] == key:
            return self._cache[1]
        placed = [(label, p) for label, p in counted if label in self._dist]
        loose = [p for label, p in counted if label not in self._dist]
        dist = np.vstack([self._dist[label] for label, _ in placed])
        near = dist <= self.spatial_m
        count = near.sum(axis=0) + len(loose)
        ok = count >= k
        if need_rx > 1:
            ok &= _spread(near, [p.receiver for _, p in placed], {p.receiver for p in loose}) >= need_rx
        if need_tx > 1:
            ok &= _spread(near, [p.sender for _, p in placed], {p.sender for p in loose}) >= need_tx
        support = None
        if ok.any():
            cost = np.where(near, dist, 0.0).sum(axis=0)
            score = np.where(ok, count * (self.spatial_m * len(placed) + 1.0) - cost, -np.inf)
            best = int(np.argmax(score))
            support = {"x": round(float(self._grid[best, 0]), 3), "y": round(float(self._grid[best, 1]), 3),
                       "links": sorted(label for (label, _), close in zip(placed, near[:, best]) if close),
                       "radius_m": float(self.spatial_m)}
        self._cache = (key, support)
        return support


def _spread(near: np.ndarray, nodes: Sequence[str], loose: set[str]) -> np.ndarray:
    """Per point, how many different nodes have a link near it; ``loose`` nodes (no segment) count everywhere."""
    rows: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        if node not in loose:
            rows.setdefault(node, []).append(i)
    total = np.full(near.shape[1], len(loose), dtype=int)
    for idx in rows.values():
        total += near[idx].any(axis=0)
    return total


def _grid_points(segments: Sequence, bounds: Optional[Sequence[float]], step: float) -> np.ndarray:
    """Points searched for the common point: a grid over ``bounds``, by default the box round the segments."""
    if bounds is None:
        xs = [x for segment in segments for x, _ in segment]
        ys = [y for segment in segments for _, y in segment]
        bounds = (min(xs), min(ys), max(xs), max(ys))
    x0, y0, x1, y1 = (float(v) for v in bounds)
    width, depth = max(x1 - x0, 0.0), max(y1 - y0, 0.0)
    step = max(step, math.sqrt(width * depth / ROOM_GRID_MAX_POINTS), 1e-3)
    gx, gy = np.meshgrid(np.linspace(x0, x1, int(math.ceil(width / step)) + 1),
                         np.linspace(y0, y1, int(math.ceil(depth / step)) + 1))
    return np.column_stack([gx.ravel(), gy.ravel()])


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
