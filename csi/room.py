"""Room geometry and a simulated person's position.

The room comes from ``config/room.json``: metres, origin at the top-left corner, x to the right and
y down. Receivers (role ``receiver``) and senders (role ``sender``, with a MAC) are the nodes. Each
(receiver, sender) pair is a link, and its motion response to a person is a Gaussian falloff of the
person's distance to the link's segment (clamped to the segment ends).

A node may carry ``"enabled": false``; it is then left out of the room altogether (no receiver or
sender, no links, nothing simulated), so a node can be defined before its board is plugged in. The
default is ``true``. Its name is kept in ``Room.disabled``.

The optional top-level ``"footprint": [[x, y], ...]`` (the key scripts/plan_layout.py reads) is the room's real
outline: a polygon of at least 3 points in metres, any winding, in the same frame. Rooms are not always rectangles
and the boards usually sit outside the walls, so the heatmap paints no heat outside it and the track stays inside
it. Without one the footprint is the room rectangle, and ``Room.footprint`` is () (a footprint that is exactly
the room rectangle is stored as () too, so such a room behaves as one without). Keys the loader does not know are
ignored.

Public API:
    load_room(path)                  -> Room (receivers and senders by name; optional link_weights, footprint)
    Room.links()                     -> [(receiver Node, sender Node)] for every pair
    Room.wall_point(side, inset)     -> (x, y) just inside a wall: "left" | "right" | "top" | "bottom"
    Room.footprint                   the outline polygon from the config, or () for the room rectangle
    Room.outline                     the footprint, or the room rectangle's corners when there is none
    Room.inside(x, y)                -> True inside the outline (or on its edge)
    parse_footprint(value, w, d)     -> validated polygon, () for the room rectangle; ValueError if unusable
    point_in_polygon(x, y, polygon)  -> True inside or on the edge (even-odd rule)
    nearest_point_in_polygon(x, y, polygon) -> (x, y) if inside, else the nearest point of the edge
    inside_mask(points, polygon)     -> bool array: which rows of points (n, 2) lie inside
    Walk(waypoints, speed_mps, loop) position(t) over time (metres)
    Stand(x, y)                      fixed position
    stand_near_wall(room, side)      Stand just inside a wall
    stand_at_centre(room)            Stand at the room centre
    link_responses(room, person, t)  {(receiver, mac): response array} over the sample times t
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

import numpy as np

# CSI_ROOM overrides the room config (tests pin it to a frozen layout so reconfiguring the real rig
# in config/room.json never changes test outcomes).
DEFAULT_ROOM_PATH = Path(os.environ.get("CSI_ROOM") or Path(__file__).resolve().parent.parent / "config" / "room.json")
FALLOFF_SIGMA_M = 0.25  # response is exp(-1/2) (about 60 %) at this distance from the segment
WALL_INSET_M = 0.25     # how far inside the wall a person stands for "near a wall"
WALLS = ("left", "right", "top", "bottom")
FOOTPRINT_TOL_M = 1e-9  # a point this close to the footprint's edge counts as inside it
Point = tuple[float, float]


@dataclass(frozen=True)
class Node:
    name: str
    role: str
    x: float
    y: float
    mac: str = ""
    chip: str = ""


@dataclass(frozen=True)
class Room:
    width_m: float
    depth_m: float
    receivers: tuple[Node, ...]
    senders: tuple[Node, ...]
    link_weights: Mapping[str, float] = field(default_factory=dict)  # link label -> weight (default 1.0)
    disabled: tuple[str, ...] = ()  # names of nodes with "enabled": false (not in receivers or senders)
    footprint: tuple[Point, ...] = ()  # the room's outline polygon, metres; () = the room rectangle

    def __post_init__(self) -> None:
        # validated and stored as a tuple of (x, y) floats; the room rectangle itself becomes ()
        footprint = parse_footprint(self.footprint, self.width_m, self.depth_m) if self.footprint else ()
        object.__setattr__(self, "footprint", footprint)

    @property
    def outline(self) -> tuple[Point, ...]:
        """The room's outline: the footprint from the config, or the room rectangle when it has none."""
        return self.footprint or rectangle(self.width_m, self.depth_m)

    def inside(self, x: float, y: float) -> bool:
        """True when (x, y) is inside the room's outline or on its edge."""
        return point_in_polygon(x, y, self.outline)

    def links(self) -> list[tuple[Node, Node]]:
        return [(rx, tx) for rx in self.receivers for tx in self.senders]

    def wall_point(self, side: str, inset: float = WALL_INSET_M) -> tuple[float, float]:
        if side == "left":
            return inset, self.depth_m / 2
        if side == "right":
            return self.width_m - inset, self.depth_m / 2
        if side == "top":
            return self.width_m / 2, inset
        if side == "bottom":
            return self.width_m / 2, self.depth_m - inset
        raise ValueError(f"unknown wall {side!r}; expected one of {WALLS}")


def load_room(path: Union[str, Path] = DEFAULT_ROOM_PATH) -> Room:
    config = json.loads(Path(path).read_text())
    nodes, disabled = [], []
    for name, spec in sorted(config["nodes"].items()):
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"node {name}: enabled must be true or false, not {enabled!r}")
        if not enabled:
            disabled.append(name)
            continue
        nodes.append(Node(name, spec["role"], float(spec["x"]), float(spec["y"]), spec.get("mac", ""),
                          spec.get("chip", "")))
    width_m, depth_m = float(config["room"]["width_m"]), float(config["room"]["depth_m"])
    footprint = config.get("footprint")
    return Room(
        width_m=width_m,
        depth_m=depth_m,
        receivers=tuple(n for n in nodes if n.role == "receiver"),
        senders=tuple(n for n in nodes if n.role == "sender"),
        link_weights={k: float(v) for k, v in config.get("link_weights", {}).items()},
        disabled=tuple(disabled),
        footprint=() if footprint is None else parse_footprint(footprint, width_m, depth_m),
    )


def rectangle(width_m: float, depth_m: float) -> tuple[Point, ...]:
    """The room rectangle as a polygon: (0, 0), (width, 0), (width, depth), (0, depth)."""
    w, d = float(width_m), float(depth_m)
    return (0.0, 0.0), (w, 0.0), (w, d), (0.0, d)


def polygon_area(polygon: Sequence[Sequence[float]]) -> float:
    """Signed area of a polygon (shoelace formula), square metres; the sign gives the winding."""
    n = len(polygon)
    return 0.5 * sum(polygon[i][0] * polygon[(i + 1) % n][1] - polygon[(i + 1) % n][0] * polygon[i][1]
                     for i in range(n))


def parse_footprint(value, width_m: Optional[float] = None, depth_m: Optional[float] = None) -> tuple[Point, ...]:
    """The room footprint from config: a list of at least 3 [x, y] points in metres, any winding.

    A closing point equal to the first is dropped. With ``width_m`` and ``depth_m``, a footprint that is just the room
    rectangle gives (), the default. Raises ValueError when it is not a polygon with an area.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"footprint must be a list of [x, y] points in metres, got {value!r}")
    points = []
    for i, p in enumerate(value):
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p)):
            raise ValueError(f"footprint point {i} must be [x, y]: two finite numbers in metres, got {p!r}")
        points.append((float(p[0]), float(p[1])))
    if len(points) > 3 and points[0] == points[-1]:
        points.pop()                                   # a closed ring: the first point repeated at the end
    if len(points) < 3:
        raise ValueError(f"footprint needs at least 3 [x, y] points, got {len(points)}")
    area = polygon_area(points)
    if abs(area) < 1e-9:
        raise ValueError("footprint has no area: its points lie on one line")
    if width_m is not None and depth_m is not None and len(points) == 4:
        corners = rectangle(width_m, depth_m)
        if (abs(abs(area) - corners[2][0] * corners[2][1]) <= 1e-9 * max(1.0, abs(area))
                and all(any(math.hypot(px - cx, py - cy) <= FOOTPRINT_TOL_M for px, py in points)
                        for cx, cy in corners)):
            return ()                                  # the room rectangle: the default
    return tuple(points)


def _nearest_on_segment(x: float, y: float, a: Sequence[float], b: Sequence[float]) -> Point:
    """The point of the segment a-b nearest (x, y)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    u = 0.0 if denom == 0 else min(1.0, max(0.0, ((x - a[0]) * dx + (y - a[1]) * dy) / denom))
    return a[0] + u * dx, a[1] + u * dy


def point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]], tol: float = FOOTPRINT_TOL_M) -> bool:
    """True when (x, y) is inside the polygon or within ``tol`` of its edge (even-odd rule, any winding)."""
    inside = False
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        nx, ny = _nearest_on_segment(x, y, a, b)
        if math.hypot(x - nx, y - ny) <= tol:
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def nearest_point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]]) -> Point:
    """(x, y) itself when it is inside the polygon (or on its edge), else the nearest point of its edge."""
    x, y = float(x), float(y)
    if point_in_polygon(x, y, polygon):
        return x, y
    best, best_d2 = (x, y), math.inf
    n = len(polygon)
    for i in range(n):
        px, py = _nearest_on_segment(x, y, polygon[i], polygon[(i + 1) % n])
        d2 = (px - x) ** 2 + (py - y) ** 2
        if d2 < best_d2:
            best, best_d2 = (px, py), d2
    return best


def inside_mask(points: np.ndarray, polygon: Sequence[Sequence[float]]) -> np.ndarray:
    """Which rows of ``points`` (n, 2) lie inside the polygon or on its edge: a bool array (n,)."""
    return np.array([point_in_polygon(float(px), float(py), polygon) for px, py in np.asarray(points, dtype=float)],
                    dtype=bool)


def point_segment_distance(points: np.ndarray, a: Sequence[float], b: Sequence[float]) -> np.ndarray:
    """Distance from each row of ``points`` (n, 2) to the segment a-b, clamped to its ends."""
    a, ab = np.asarray(a, dtype=float), np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    denom = float(ab @ ab)
    u = np.zeros(len(points)) if denom == 0 else np.clip((points - a) @ ab / denom, 0.0, 1.0)
    closest = a + u[:, None] * ab
    return np.linalg.norm(points - closest, axis=1)


def link_responses(room: Room, person: Optional["Person"], t: np.ndarray,
                   gain: float = 1.0) -> dict[tuple[str, str], np.ndarray]:
    """Per link, the response in [0, gain] at each sample time: gain * exp(-d^2 / (2 sigma^2))."""
    n = len(t)
    if person is None:
        return {(rx.name, tx.mac): np.zeros(n) for rx, tx in room.links()}
    positions = person.position(t)
    out = {}
    for rx, tx in room.links():
        d = point_segment_distance(positions, (rx.x, rx.y), (tx.x, tx.y))
        out[(rx.name, tx.mac)] = gain * np.exp(-d ** 2 / (2 * FALLOFF_SIGMA_M ** 2))
    return out


@dataclass(frozen=True)
class Walk:
    """Walk through ``waypoints`` at ``speed_mps``. Starts at the first waypoint at t = 0.

    ``loop`` returns to the first waypoint and repeats; otherwise the person stops at the last one.
    """
    waypoints: tuple[tuple[float, float], ...]
    speed_mps: float = 0.5
    loop: bool = False

    def position(self, t: np.ndarray) -> np.ndarray:
        pts = np.asarray(self.waypoints, dtype=float)
        if self.loop:
            pts = np.vstack([pts, pts[:1]])
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = cum[-1]
        s = np.asarray(t, dtype=float) * self.speed_mps
        if total == 0:
            s = np.zeros_like(s)
        elif self.loop:
            s = np.mod(s, total)
        else:
            s = np.minimum(s, total)
        return np.column_stack([np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])])


@dataclass(frozen=True)
class Stand:
    """A person who does not move."""
    x: float
    y: float

    def position(self, t: np.ndarray) -> np.ndarray:
        return np.tile([self.x, self.y], (len(t), 1)).astype(float)


Person = Union[Walk, Stand]


def stand_near_wall(room: Room, side: str, inset: float = WALL_INSET_M) -> Stand:
    return Stand(*room.wall_point(side, inset))


def stand_at_centre(room: Room) -> Stand:
    return Stand(room.width_m / 2, room.depth_m / 2)
