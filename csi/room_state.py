"""Room state for the transceiver pipeline: zones, and agreement among links that do not share a node.

The hackathon rule (``RoomDecision``, csi/adaptive.py: two links elevated, or one strongly) does not scale. With
870 links, two of them are elevated by chance far more often than with four, and the ten links of one node are
raised together by that node's own fault (a front-end glitch, a gain step), so they are one witness, not ten.
Report 08 (docs/research/scaling-redo/08-software-redo.md, section 2) gives the numbers.

``RoomState`` decides instead where the links agree, and counts only links that share no node:

Geometry (``ZoneMap``, precomputed per layout)
    zones         the room split into a grid of squares of about ``zone_m``, named like a chessboard: columns
                  A, B, ... along x, rows 1, 2, ... along y, so "A1" is the top-left zone (y runs down).
    probe points  a grid of points every ``probe_m`` inside each zone.
    covers        a link covers a probe point when its segment passes within ``cover_m`` of it: a person at the
                  point would raise that link.

Independent links
    Links that share a node are not independent. The independent links of a set are counted as the size of a
    largest subset in which no two links share a node (a matching in the graph whose vertices are nodes and whose
    edges are links); both directions of one node pair are one edge. ``k`` faulty nodes can raise at most ``k``
    independent links, however many links they are on. The count is greedy (fewest-shared-nodes first), a lower
    bound of the true matching.

Decision, at each update (host time ``t``)
    warm link       a link with a warm reading heard within ``no_signal_s``; elevated = warm and elevated.
    capacity(p)     independent links among the warm links covering probe point p.
    need(p)         max(m, min(target_independent, capacity(p)), ceil(fraction x capacity(p))), with
                    m = min(min_independent, independent warm links in the whole room): at least two (three where
                    three can cover p), and a third of the capacity. It grows with the number of links crossing p,
                    so the required agreement scales with the link count and the layout instead of a fixed two. A
                    point with capacity below m has no independent view and never fires (its neighbours do); a 2- or
                    3-node rig has no two independent links at all, so there m = 1.
    fires(p)        the elevated links covering p hold at least need(p) independent links.
    zone            MOTION while one of its probe points fires, and for ``hold_s`` after the last one stopped.
    room            WARMUP with no warm link, else MOTION while any zone is MOTION, else EMPTY.

Which way the trade-off goes (ADR-0001, a miss costs more; docs/adr/0024-adaptive-baseline-only-and-a-room-state-
from-zones.md): ``fraction`` is low, because a person raises nearly every link that covers the point they stand on
(the falloff is 0.25 m, the cover radius 0.3 m), so asking for a third of them costs little sensitivity and removes
most chance agreement; dead or warming links lower the capacity and with it the need; and a rig with no independent
view at all (2 or 3 nodes) is judged on a single link. The cost accepted: a person seen only by the links of one
node (next to a corner node of a sparse rig) is not reported until a link of another node sees them too.

Public API:
    ZoneMap(room, links, zone_m, probe_m, cover_m)   links: [(label, receiving Node, transmitting Node)]
    ZoneMap.zones                                    (Zone(id, x0, y0, x1, y1), ...)
    RoomState(zone_map, ...)
    RoomState.observe(label, t, reading)             one link's latest Reading (None: no reading, e.g. NO_SIGNAL)
    RoomState.update(t) -> state                     the room state at host time t; once per round broadcast
    RoomState.state, .zone_states, .elevated, .motion_zones
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .adaptive import EMPTY, MOTION, WARMUP, Reading
from .room import Node, Room, point_segment_distance

ZONE_M = 1.0            # zone side: the unit the operator reads and the possibly-occupied hold (#9) latches on
PROBE_M = 0.25          # spacing of the probe points inside a zone
COVER_M = 0.3           # a link covers a probe point within this distance of its segment
FRACTION = 1 / 3        # need = this share of the independent links covering a point (at least MIN_INDEPENDENT)
MIN_INDEPENDENT = 2     # the least agreement: two links that share no node ...
TARGET_INDEPENDENT = 3  # ... three where three independent links cover the point
HOLD_S = 1.0            # a zone (and so the room) stays MOTION this long after the last firing point
NO_SIGNAL_S = 2.0       # a link not heard for longer is not warm


@dataclass(frozen=True)
class Zone:
    id: str
    x0: float
    y0: float
    x1: float
    y1: float


def _zone_name(col: int, row: int) -> str:
    letters = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{letters}{row + 1}"


class ZoneMap:
    """Zones, probe points and which links cover which point, for one layout (see the module doc)."""

    def __init__(self, room: Room, links: Sequence[tuple[str, Node, Node]], *, zone_m: float = ZONE_M,
                 probe_m: float = PROBE_M, cover_m: float = COVER_M) -> None:
        cols = max(1, round(room.width_m / zone_m))
        rows = max(1, round(room.depth_m / zone_m))
        zw, zd = room.width_m / cols, room.depth_m / rows
        per_x, per_y = max(1, round(zw / probe_m)), max(1, round(zd / probe_m))
        zones, points, zone_of = [], [], []
        for row in range(rows):
            for col in range(cols):
                x0, y0 = col * zw, row * zd
                zones.append(Zone(_zone_name(col, row), x0, y0, x0 + zw, y0 + zd))
                for i in range(per_x):
                    for j in range(per_y):
                        points.append((x0 + (i + 0.5) * zw / per_x, y0 + (j + 0.5) * zd / per_y))
                        zone_of.append(len(zones) - 1)
        self.zones: tuple[Zone, ...] = tuple(zones)
        self.points = np.array(points, dtype=float)
        self.zone_of = np.array(zone_of)
        self.labels: tuple[str, ...] = tuple(label for label, _, _ in links)
        names = sorted({n.name for _, rx, tx in links for n in (rx, tx)})
        bit = {name: 1 << i for i, name in enumerate(names)}
        self.masks: tuple[int, ...] = tuple(bit[rx.name] | bit[tx.name] for _, rx, tx in links)  # the link's nodes
        cover = np.zeros((len(points), len(links)), dtype=bool)
        for k, (_, rx, tx) in enumerate(links):
            cover[:, k] = point_segment_distance(self.points, (rx.x, rx.y), (tx.x, tx.y)) <= cover_m
        self.cover = cover
        self.covering: tuple[np.ndarray, ...] = tuple(np.flatnonzero(cover[p]) for p in range(len(points)))


def independent_links(masks: Sequence[int], stop_at: Optional[int] = None) -> int:
    """How many of these links share no node (each link given as the bitmask of its two nodes); see the module doc.

    Greedy: links whose nodes are on the fewest of the others go first. Stops counting at ``stop_at``.
    """
    edges = set(masks)
    if not edges:
        return 0
    degree: dict[int, int] = {}
    for m in edges:
        for b in _bits(m):
            degree[b] = degree.get(b, 0) + 1
    used, count = 0, 0
    for m in sorted(edges, key=lambda m: (sum(degree[b] for b in _bits(m)), m)):
        if not used & m:
            used |= m
            count += 1
            if stop_at is not None and count >= stop_at:
                break
    return count


def _bits(mask: int) -> tuple[int, ...]:
    out = []
    while mask:
        low = mask & -mask
        out.append(low)
        mask ^= low
    return tuple(out)


class RoomState:
    """The room-level decision from zones and independent links (see the module doc)."""

    def __init__(self, zone_map: ZoneMap, *, fraction: float = FRACTION, min_independent: int = MIN_INDEPENDENT,
                 hold_s: float = HOLD_S, no_signal_s: float = NO_SIGNAL_S,
                 target_independent: int = TARGET_INDEPENDENT) -> None:
        self.map = zone_map
        self.target_independent = target_independent
        self.fraction, self.min_independent, self.hold_s, self.no_signal_s = fraction, min_independent, hold_s, no_signal_s
        n = len(zone_map.labels)
        self._index = {label: k for k, label in enumerate(zone_map.labels)}
        self._warm = np.zeros(n, dtype=bool)
        self._elevated = np.zeros(n, dtype=bool)
        self._heard = np.full(n, -np.inf)
        self._masks = np.array(zone_map.masks, dtype=np.int64)
        self._last_warm: Optional[np.ndarray] = None
        self._last_elevated: Optional[np.ndarray] = None
        self._capacity: dict[int, int] = {}       # probe point -> capacity, for the current warm set
        self._room_m = 0                          # m: min(min_independent, independent warm links in the room)
        self._firing = np.zeros(len(zone_map.zones), dtype=bool)
        self._last_fired = np.full(len(zone_map.zones), -np.inf)
        self._live = np.zeros(n, dtype=bool)
        self._t: Optional[float] = None
        self.state = WARMUP

    # ------------------------------------------------------------------ input

    def observe(self, label: str, t: float, reading: Optional[Reading]) -> None:
        """A link's latest reading at host time ``t``; ``None`` when it has none (NO_SIGNAL). Unknown labels are ignored."""
        k = self._index.get(label)
        if k is None:
            return
        self._heard[k] = t
        self._warm[k] = reading is not None and reading.warm
        self._elevated[k] = reading is not None and reading.warm and reading.elevated

    def update(self, t: float) -> str:
        """The room state at host time ``t``, from the readings observed so far."""
        self._t = t
        live = self._warm & (t - self._heard <= self.no_signal_s)
        elevated = self._elevated & live
        self._live = live
        if self._last_warm is None or not np.array_equal(live, self._last_warm):
            self._last_warm = live.copy()
            self._capacity.clear()
            self._room_m = min(self.min_independent, independent_links(self._masks[live].tolist(), self.min_independent))
            self._last_elevated = None
        if self._last_elevated is None or not np.array_equal(elevated, self._last_elevated):
            self._last_elevated = elevated.copy()
            self._firing = self._fire(elevated)
        self._last_fired = np.where(self._firing, t, self._last_fired)
        if not live.any():
            self.state = WARMUP
        else:
            self.state = MOTION if self._zone_motion(t).any() else EMPTY
        return self.state

    # ------------------------------------------------------------------ output

    @property
    def zone_states(self) -> dict[str, str]:
        """{zone id: MOTION, EMPTY or WARMUP} at the last update (WARMUP: no warm link covers the zone)."""
        motion = self._zone_motion(self._t) if self._t is not None else np.zeros(len(self.map.zones), dtype=bool)
        covered = np.zeros(len(self.map.zones), dtype=bool)
        if self._live.any():
            np.logical_or.at(covered, self.map.zone_of, self.map.cover[:, self._live].any(axis=1))
        return {z.id: MOTION if motion[i] else (EMPTY if covered[i] else WARMUP) for i, z in enumerate(self.map.zones)}

    @property
    def motion_zones(self) -> list[str]:
        if self._t is None:
            return []
        motion = self._zone_motion(self._t)
        return [z.id for i, z in enumerate(self.map.zones) if motion[i]]

    @property
    def elevated(self) -> list[str]:
        """Labels of the elevated warm links at the last update, sorted."""
        if self._last_elevated is None:
            return []
        return sorted(self.map.labels[k] for k in np.flatnonzero(self._last_elevated))

    # ------------------------------------------------------------------ internals

    def _zone_motion(self, t: float) -> np.ndarray:
        return self._firing | (t - self._last_fired < self.hold_s)

    def _fire(self, elevated: np.ndarray) -> np.ndarray:
        firing = np.zeros(len(self.map.zones), dtype=bool)
        m = self._room_m
        if m == 0 or int(elevated.sum()) < m:
            return firing
        counts = self.map.cover[:, elevated].sum(axis=1)
        for p in np.flatnonzero(counts >= m):
            zone = self.map.zone_of[p]
            if firing[zone]:
                continue
            capacity = self._point_capacity(p)
            if capacity < m:
                continue
            need = self._need(capacity, m)
            if counts[p] < need:
                continue
            links = self.map.covering[p]
            masks = self._masks[links[elevated[links]]].tolist()
            if independent_links(masks, need) >= need:
                firing[zone] = True
        return firing

    def _need(self, capacity: int, m: int) -> int:
        return max(m, min(self.target_independent, capacity), math.ceil(self.fraction * capacity))

    def _point_capacity(self, p: int) -> int:
        if p not in self._capacity:
            links = self.map.covering[p]
            self._capacity[p] = independent_links(self._masks[links[self._last_warm[links]]].tolist())
        return self._capacity[p]
