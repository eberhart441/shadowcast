"""Room geometry and a simulated person's position.

The room comes from ``config/room.json``: metres, origin at the top-left corner, x to the right and
y down. Nodes sit on a plane of ``PLANE_M`` x ``PLANE_M`` (x and y from 0 to 10 m). The room's working area,
the rectangle the heatmap, zones, tracker and simulated walks use, is not configured: it is the bounding box of
the enabled nodes, padded by ``AREA_MARGIN_M`` on every side, clamped to the plane, at least ``AREA_MIN_M`` on a
side (``working_area``). It need not start at (0, 0): ``Room.x0_m``, ``Room.y0_m`` are its top-left corner and
``width_m``, ``depth_m`` its size. A config's ``"room": {"width_m", "depth_m"}`` is optional and ignored (older
files carry it). A node from an older file may sit off the plane; it loads, outside the area. Receivers (role ``receiver``) and senders (role ``sender``, with a MAC) are the nodes of the
hackathon rig; transceivers (role ``transceiver``, the default when a node gives no role) both send and
receive round broadcasts and are told apart by their board serial. Each (receiver, sender) pair is a link,
and so is each ordered pair of distinct transceivers (``Room.transceiver_links``), and its motion response to a person is a Gaussian falloff of the
person's distance to the link's segment (clamped to the segment ends).

A node may carry ``"enabled": false``; it is then left out of the room altogether (no receiver or
sender, no links, nothing simulated), so a node can be defined before its board is plugged in. The
default is ``true``. Its name is kept in ``Room.disabled``.

Public API:
    load_room(path)                  -> Room (receivers and senders by name; optional link_weights)
    room_from_config(obj) / room_config(room)   Room from / to a parsed room.json object (round recordings keep one)
    working_area(nodes)              -> (x0, y0, width, depth): the area around these nodes (see above)
    move_node(room, name, x, y)      -> a new Room with that node at (x, y), metres rounded to 3 decimals, and the
                                        working area refitted; ValueError for an unknown or disabled node, or a
                                        position that is not a finite number on the plane
    save_node_position(path, name, x, y)   write a node's x, y into a room file atomically, keeping everything else
    Room.links()                     -> [(receiver Node, sender Node)] for every pair
    Room.transceiver_links()         -> [(receiving Node, transmitting Node)] for every ordered transceiver pair
    Room.wall_point(side, inset)     -> (x, y) just inside a wall: "left" | "right" | "top" | "bottom"
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
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

import numpy as np

# CSI_ROOM overrides the room config (tests pin it to a frozen layout so reconfiguring the real rig
# in config/room.json never changes test outcomes).
DEFAULT_ROOM_PATH = Path(os.environ.get("CSI_ROOM") or Path(__file__).resolve().parent.parent / "config" / "room.json")
FALLOFF_SIGMA_M = 0.25  # response is exp(-1/2) (about 60 %) at this distance from the segment
STOP_FADE_S = 1.5       # a person who stops dead leaves the score within this (high-pass plus 1 s window)
WALL_INSET_M = 0.25     # how far inside the wall a person stands for "near a wall"
WALLS = ("left", "right", "top", "bottom")
POSITION_DECIMALS = 3   # a moved node's x, y in metres: millimetres
PLANE_M = 10.0          # nodes sit on x, y in [0, PLANE_M] metres; the working area never leaves it
AREA_MARGIN_M = 0.5     # the working area reaches this far beyond the outermost nodes
AREA_MIN_M = 1.0        # and is at least this wide and deep
TRANSCEIVER = "transceiver"
ROLES = ("receiver", "sender", TRANSCEIVER)


def board_serial(value: Optional[str]) -> str:
    """A board serial (base MAC) in its one form, upper-case hex in colon pairs: ``38:44:be:a5:33:9c``,
    ``3844BEA5339C`` and ``38:44:BE:A5:33:9C`` are the same board. Anything not 6 bytes long keeps its hex digits
    only (``csi.ports.normalise_serial`` is this without the colons)."""
    digits = re.sub(r"[^0-9A-F]", "", (value or "").upper())
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)) if len(digits) == 12 else digits


@dataclass(frozen=True)
class Node:
    name: str
    role: str
    x: float
    y: float
    mac: str = ""
    chip: str = ""
    serial: str = ""  # the board's USB serial, its base MAC in colon form (identifies the board; see csi/ports.py)


@dataclass(frozen=True)
class Room:
    width_m: float
    depth_m: float
    receivers: tuple[Node, ...]
    senders: tuple[Node, ...]
    link_weights: Mapping[str, float] = field(default_factory=dict)  # link label -> weight (default 1.0)
    disabled: tuple[str, ...] = ()  # names of nodes with "enabled": false (not in receivers or senders)
    transceivers: tuple[Node, ...] = ()  # nodes that both send and receive round broadcasts
    x0_m: float = 0.0   # the working area's top-left corner (width_m, depth_m are its size)
    y0_m: float = 0.0

    @property
    def area(self) -> tuple[float, float, float, float]:
        """(x0, y0, width, depth) of the working area, metres."""
        return self.x0_m, self.y0_m, self.width_m, self.depth_m

    def nodes(self) -> tuple[Node, ...]:
        """Every enabled node: receivers, senders, then transceivers."""
        return (*self.receivers, *self.senders, *self.transceivers)

    def links(self) -> list[tuple[Node, Node]]:
        return [(rx, tx) for rx in self.receivers for tx in self.senders]

    def transceiver_serials(self) -> dict[str, str]:
        """{transceiver name: board serial (``board_serial`` form)} for every transceiver with a serial."""
        return {node.name: board_serial(node.serial) for node in self.transceivers if node.serial}

    def transceiver_names(self) -> dict[str, str]:
        """{board serial: transceiver name}: the one way to name a node from a serial a frame or a report carries
        (look it up with ``board_serial``)."""
        return {serial: name for name, serial in self.transceiver_serials().items()}

    def transceiver_links(self) -> list[tuple[Node, Node]]:
        """(receiving node, transmitting node) for every ordered pair of distinct transceivers: N(N-1) links."""
        return [(rx, tx) for rx in self.transceivers for tx in self.transceivers if rx is not tx]

    def wall_point(self, side: str, inset: float = WALL_INSET_M) -> tuple[float, float]:
        cx, cy = self.x0_m + self.width_m / 2, self.y0_m + self.depth_m / 2
        if side == "left":
            return self.x0_m + inset, cy
        if side == "right":
            return self.x0_m + self.width_m - inset, cy
        if side == "top":
            return cx, self.y0_m + inset
        if side == "bottom":
            return cx, self.y0_m + self.depth_m - inset
        raise ValueError(f"unknown wall {side!r}; expected one of {WALLS}")


def load_room(path: Union[str, Path] = DEFAULT_ROOM_PATH) -> Room:
    return room_from_config(json.loads(Path(path).read_text()))


def working_area(nodes: Sequence[Node]) -> tuple[float, float, float, float]:
    """(x0, y0, width, depth): the bounding box of ``nodes`` padded by ``AREA_MARGIN_M``, clamped to the plane
    [0, ``PLANE_M``], and grown (about its centre, then away from a plane edge) to at least ``AREA_MIN_M`` a side.
    Without nodes, the ``AREA_MIN_M`` square at the plane's origin."""
    out = []
    for values in ([n.x for n in nodes], [n.y for n in nodes]):
        if not values:
            out.append((0.0, AREA_MIN_M))
            continue
        lo, hi = min(values) - AREA_MARGIN_M, max(values) + AREA_MARGIN_M
        if hi - lo < AREA_MIN_M:
            mid = (lo + hi) / 2
            lo, hi = mid - AREA_MIN_M / 2, mid + AREA_MIN_M / 2
        lo, hi = min(max(lo, 0.0), PLANE_M), min(max(hi, 0.0), PLANE_M)
        if hi - lo < AREA_MIN_M:                 # clamped against an edge of the plane: grow the other way
            lo, hi = (lo, lo + AREA_MIN_M) if lo + AREA_MIN_M <= PLANE_M else (PLANE_M - AREA_MIN_M, PLANE_M)
        out.append((round(lo, POSITION_DECIMALS), round(hi - lo, POSITION_DECIMALS)))
    (x0, width), (y0, depth) = out
    return x0, y0, width, depth


def fit_area(room: Room) -> Room:
    """``room`` with its working area fitted to its enabled nodes (``working_area``)."""
    x0, y0, width, depth = working_area(room.nodes())
    return replace(room, x0_m=x0, y0_m=y0, width_m=width, depth_m=depth)


def room_from_config(config: Mapping) -> Room:
    """A Room from a parsed ``config/room.json`` object. A ``"room"`` size in it is ignored: the working area is
    fitted to the enabled nodes."""
    nodes, disabled = [], []
    for name, spec in sorted(config["nodes"].items()):
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"node {name}: enabled must be true or false, not {enabled!r}")
        if not enabled:
            disabled.append(name)
            continue
        role = spec.get("role", TRANSCEIVER)
        if role not in ROLES:
            raise ValueError(f"node {name}: role must be one of {', '.join(ROLES)}, not {role!r}")
        nodes.append(Node(name, role, float(spec["x"]), float(spec["y"]), spec.get("mac", ""),
                          spec.get("chip", ""), spec.get("serial", "")))
    return fit_area(Room(
        width_m=AREA_MIN_M,
        depth_m=AREA_MIN_M,
        receivers=tuple(n for n in nodes if n.role == "receiver"),
        senders=tuple(n for n in nodes if n.role == "sender"),
        link_weights={k: float(v) for k, v in config.get("link_weights", {}).items()},
        disabled=tuple(disabled),
        transceivers=tuple(n for n in nodes if n.role == TRANSCEIVER),
    ))


def room_config(room: Room) -> dict:
    """``room`` as a ``config/room.json`` object: ``room_from_config(room_config(room)) == room`` for a room whose
    area is fitted to its nodes (every loaded room). No room size is written: the area follows the nodes.

    A disabled node keeps only its name (``"enabled": false``); its position is not in the Room.
    """
    nodes: dict[str, dict] = {}
    for node in (*room.receivers, *room.senders, *room.transceivers):
        spec: dict = {"role": node.role, "x": node.x, "y": node.y}
        spec.update((key, value) for key, value in (("mac", node.mac), ("chip", node.chip), ("serial", node.serial))
                    if value)
        nodes[node.name] = spec
    nodes.update((name, {"enabled": False}) for name in room.disabled)
    config: dict = {"nodes": nodes}
    if room.link_weights:
        config["link_weights"] = dict(room.link_weights)
    return config


def move_node(room: Room, name: str, x: float, y: float) -> Room:
    """``room`` with node ``name`` at (x, y) in metres, rounded to ``POSITION_DECIMALS``, and the working area
    refitted to the nodes (``working_area``); everything else is kept.

    Raises ValueError for an unknown or disabled node, or a position that is not a finite number on the plane
    (x and y from 0 to ``PLANE_M``).
    """
    if name in room.disabled:
        raise ValueError(f"node {name!r} is disabled in the room config (\"enabled\": false): it is not on the map")
    groups = {"receivers": room.receivers, "senders": room.senders, "transceivers": room.transceivers}
    where = next((key for key, nodes in groups.items() if any(n.name == name for n in nodes)), None)
    if where is None:
        names = sorted(n.name for nodes in groups.values() for n in nodes)
        raise ValueError(f"unknown node {name!r}; nodes are {', '.join(names)}")
    for axis, value in (("x", x), ("y", y)):
        if not math.isfinite(value):
            raise ValueError(f"{axis} must be a finite number of metres, got {value!r}")
        if not 0.0 <= value <= PLANE_M:
            raise ValueError(f"{axis} = {value:g} m is off the plane: nodes sit within 0 to {PLANE_M:g} m")
    x, y = round(float(x), POSITION_DECIMALS), round(float(y), POSITION_DECIMALS)
    moved = tuple(replace(n, x=x, y=y) if n.name == name else n for n in groups[where])
    return fit_area(replace(room, **{where: moved}))


def save_node_position(path: Union[str, Path], name: str, x: float, y: float) -> None:
    """Set node ``name``'s ``x`` and ``y`` (rounded to ``POSITION_DECIMALS``) in the room file at ``path``.

    Every other field, the key order, the 2-space JSON layout and the line endings are kept. The file is written to a
    temporary file next to it and moved over it, so a reader never sees half a file. Raises KeyError when the file
    has no such node, OSError when it cannot be read or written, ValueError when it is not JSON.
    """
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    config = json.loads(text)
    spec = config.get("nodes", {}).get(name)
    if not isinstance(spec, dict):
        raise KeyError(f"{path} has no node {name!r}")
    spec["x"], spec["y"] = round(float(x), POSITION_DECIMALS), round(float(y), POSITION_DECIMALS)
    out = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    if "\r\n" in text:
        out = out.replace("\n", "\r\n")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(out.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    positions, moving = person.position(t), movement(person, t)
    out = {}
    for rx, tx in room.links():
        d = point_segment_distance(positions, (rx.x, rx.y), (tx.x, tx.y))
        out[(rx.name, tx.mac)] = gain * moving * np.exp(-d ** 2 / (2 * FALLOFF_SIGMA_M ** 2))
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


@dataclass(frozen=True)
class WalkAndStop:
    """Walk through ``waypoints`` at ``speed_mps``, then stand still at the last one, raising no link.

    ``Walk`` and ``Stand`` raise the links near them for as long as they are there, like a person who keeps moving
    slightly in place; the adaptive baseline absorbs that in about 30 s. This person instead stops dead: a still
    body is static to the score's high-pass, so the links' response fades out over ``STOP_FADE_S`` after the stop
    (docs/theory.md, section 11: one to two seconds). The simulators scale the response by ``moving(t)``.
    """
    waypoints: tuple[tuple[float, float], ...]
    speed_mps: float = 0.5

    @property
    def stop_s(self) -> float:
        """Time the person reaches the last waypoint and stops."""
        pts = np.asarray(self.waypoints, dtype=float)
        return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()) / self.speed_mps

    def position(self, t: np.ndarray) -> np.ndarray:
        return Walk(self.waypoints, self.speed_mps).position(t)

    def moving(self, t: np.ndarray) -> np.ndarray:
        """How much the person still moves at each time, 1 (walking) to 0 (still): falls linearly after the stop."""
        return np.clip(1.0 - (np.asarray(t, dtype=float) - self.stop_s) / STOP_FADE_S, 0.0, 1.0)


Person = Union[Walk, Stand, WalkAndStop]


def movement(person: "Person", t: np.ndarray) -> np.ndarray:
    """How much ``person`` moves at each time, 0..1: their ``moving(t)``, else 1 (the response follows position)."""
    moving = getattr(person, "moving", None)
    return np.ones(len(t)) if moving is None else moving(t)


def stand_near_wall(room: Room, side: str, inset: float = WALL_INSET_M) -> Stand:
    return Stand(*room.wall_point(side, inset))


def stand_at_centre(room: Room) -> Stand:
    return Stand(room.x0_m + room.width_m / 2, room.y0_m + room.depth_m / 2)
