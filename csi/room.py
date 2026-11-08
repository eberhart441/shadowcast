"""Room geometry and a simulated person's position.

The room comes from ``config/room.json``: metres, origin at the top-left corner, x to the right and
y down. Receivers (role ``receiver``) and senders (role ``sender``, with a MAC) are the nodes. Each
(receiver, sender) pair is a link, and its motion response to a person is a Gaussian falloff of the
person's distance to the link's segment (clamped to the segment ends).

A node may carry ``"enabled": false``; it is then left out of the room altogether (no receiver or
sender, no links, nothing simulated), so a node can be defined before its board is plugged in. The
default is ``true``. Its name is kept in ``Room.disabled``.

Public API:
    load_room(path)                  -> Room (receivers and senders by name; optional link_weights)
    Room.links()                     -> [(receiver Node, sender Node)] for every pair
    Room.wall_point(side, inset)     -> (x, y) just inside a wall: "left" | "right" | "top" | "bottom"
    Walk(waypoints, speed_mps, loop) position(t) over time (metres)
    Stand(x, y)                      fixed position
    stand_near_wall(room, side)      Stand just inside a wall
    stand_at_centre(room)            Stand at the room centre
    link_responses(room, person, t)  {(receiver, mac): response array} over the sample times t
"""
from __future__ import annotations

import json
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
    return Room(
        width_m=float(config["room"]["width_m"]),
        depth_m=float(config["room"]["depth_m"]),
        receivers=tuple(n for n in nodes if n.role == "receiver"),
        senders=tuple(n for n in nodes if n.role == "sender"),
        link_weights={k: float(v) for k, v in config.get("link_weights", {}).items()},
        disabled=tuple(disabled),
    )


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
