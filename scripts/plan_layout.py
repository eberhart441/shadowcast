#!/usr/bin/env python
"""Plan where the boards go: link geometry, signal budget, serial load and coverage of a layout.

    plan_layout.py config/examples/three_room_row.json               # readable report
    plan_layout.py config/examples/l_house.json --mode transceiver   # every board sends and receives
    plan_layout.py config/room.json --json > plan.json               # everything as JSON
    plan_layout.py layout.json --grid 0.5 --band 0.8                 # coarser grid, wider bands

Once the code is right, placement decides what the boards can see: surround the area (at least 3 sides, ideally
4), make the links cross the rooms, keep the boards about 0.75-1 m outside the watched area, and space them closely
enough for the accuracy you need. This checks a layout against those rules before anything is mounted.

INPUT. A room config in csi/room.py's schema: metres, origin at the top-left corner, x to the right, y down;
"room": {"width_m", "depth_m"}; "nodes": {name: {"role": "receiver" | "sender", "x", "y", ...}}; a node with
"enabled": false is left out. North is the top of the map (small y) and east the right, as on the dashboard.
Optional keys that only this tool reads (csi/room.py ignores them):

    "footprint": [[x, y], ...]    the building or floor section being watched, as a polygon (default: the room
                                  rectangle). Boards normally sit outside it.
    "zones": {"name": [x0, y0, x1, y1] or [[x, y], ...]}     named areas (rooms) to report on.
    "walls": [{"a": [x, y], "b": [x, y], "db": 10}, ...]      walls a link loses signal through (one way, 2.4 GHz).
                                  A wall may give "material" instead of "db". Typical losses:
                                    drywall 3-5 dB (4 if named), brick 8-12 (10), concrete block 10-15 (12),
                                    reinforced concrete 15-25 or more (20), glass about 3, solid wood door about 6.
    "send_hz": 20                 the senders' CSI_SEND_HZ (default 20).

MODES. --mode fixed (default, today's firmware): every receiver hears every sender, one link per pair.
--mode transceiver (the planned round-robin): every board sends and receives, one link per ordered pair. The two
directions of a pair share one path, which counts once on the coverage map and is listed once in the report.

PER LINK. Length; predicted RSSI = -32 dBm at 2 m (measured on the hw5 link) - 20 log10(d / 2 m) - the loss of
every wall it crosses (--exponent changes the 20 = 10 x 2); flags:
    SAME_SIDE  both boards on the same side of the footprint (each within 1.5 m outside one edge, or at most
               0.3 m inside it) and the link within 30 degrees of that edge: it runs along the wall and carries
               almost no information about the interior.
    OUTSIDE    more than 30% of the link lies outside the footprint, not counting up to 1.5 m at each end (the
               boards are meant to stand outside): for example a link across the notch of an L-shaped house, or
               one that only cuts a corner. That part sees nothing inside.
    WEAK       predicted RSSI below -70 dBm: expect a noisy score; it needs a 2-3 s window or a better antenna.
    SHORT      under 1.5 m: it mostly senses the space around its two boards.
A board exactly on a wall line does not count that wall: put boards off the line to count it.

PER RECEIVER. A receiver forces one gain for all its senders, so when their RSSI differs by more than about 6 dB
the near links clip and the far ones quantise: equalise the distances or build it with CSI_GAIN_LOCK_STRONGEST=1
(experimental). Serial load = senders heard x real rate, where the real rate is f / (1 + 0.001 f) packets/s (the
sender's sleep adds 1 ms per period: 19.6 at 20 Hz, 24.4 at 25, 38.5 at 40), against about 107 lines/s for
full-width CSI lines at 921600 baud; plan for at most about 80.

COVERAGE. A grid over the footprint (cells whose centre is inside it, --grid metres). A link covers a cell when the
centre lies in its sensitive band: within 0.6 m + 5% of the link length of its line (--band, --band-per-m), between
its two ends. There are no end caps: motion right next to a board moves all of that board's links together and
says nothing about where in the room it is. A cell covered by fewer than 3 links (--min-links) is blind: placing a
person needs at least three crossing links. Well conditioned: 3 or more links, two of them crossing at 45 degrees or
more. A zone counts the links that cover at least 25% of its cells, and the distinct receivers and senders among
them. A side (north, east, south, west) is covered when a board sits within 1.5 m outside a footprint edge facing
that way (a board on the wall itself counts). A link weight of 0 in "link_weights" leaves that link off the map.

ACCURACY. From the perimeter spacing (footprint perimeter / boards), as in the radio tomography literature for one
moving person with every board sending and receiving: about 1 m -> sub-metre, 2 m -> 1-1.5 m, 3 m -> room level.
One class worse with more than 25% blind cells; capped at room level with boards on fewer than 3 sides (along the
links, error is then no better than a guess) or fewer than 10 boards.

SUGGESTIONS. Ranked, most important first. To add or move a board the tool tries positions every 0.5 m (coarser
on big footprints) on a ring --offset (0.75 m) outside the footprint and keeps the one that leaves the fewest blind
cells. Additions are planned greedily, up to three boards one after another, each on top of the ones before.

These are geometric rules of thumb from the literature and the team's measurements, not a propagation model: walls,
furniture and multipath change real links, so walk-test every link before trusting it. Standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

REF_DBM = -32.0             # measured at 2 m on the hw5 link (C5 receiver, S3 sender)
REF_M = 2.0
PATH_LOSS_EXPONENT = 2.0    # 20 log10(d): free space; cluttered indoor paths fall off faster
MIN_DISTANCE_M = 0.1        # the RSSI model is clamped here
WEAK_DBM = -70.0
SHORT_M = 1.5
OUTSIDE_SHARE = 0.30
END_ALLOWANCE_M = 1.5       # outside length at each end of a link that OUTSIDE does not count
SIDE_OUT_M = 1.5            # a board up to this far outside a footprint edge is on that side
SIDE_IN_M = 0.3             # ... or up to this far inside it (on the wall's inner face)
SIDE_MARGIN_M = 1.0         # ... and up to this far past the edge's ends (corner boards)
SAME_SIDE_ANGLE_DEG = 30.0
SPREAD_WARN_DB = 6.0
LINE_CAP_PER_S = 107.0
LOAD_WARN_PER_S = 80.0
SEND_HZ = 20.0
MIN_SEND_HZ = 20.0          # calibration refuses a link under 15 packets/s, so do not go below 20 Hz
GRID_M = 0.25
BAND_M = 0.6
BAND_PER_M = 0.05
MIN_LINKS = 3
CROSS_DEG = 45.0
ZONE_SHARE = 0.25
BLIND_DOWNGRADE_SHARE = 0.25
RING_OFFSET_M = 0.75
RING_STEP_M = 0.5
MAX_CANDIDATES = 120        # the ring step grows on big footprints to stay near this many positions
GREEDY_STEPS = 3
NEAR_EDGE_M = 0.5
FAR_OUT_M = 2.5
MIN_BOARDS_FINE = 10
MAX_GRID_CELLS = 400_000
MAX_TABLE_ROWS = 40
MAP_MAX_COLS = 110
WRAP = 110

MATERIAL_DB = {"drywall": 4.0, "brick": 10.0, "concrete_block": 12.0, "reinforced_concrete": 20.0, "glass": 3.0,
               "wood_door": 6.0}
COMPASS = (("north", (0.0, -1.0)), ("east", (1.0, 0.0)), ("south", (0.0, 1.0)), ("west", (-1.0, 0.0)))
COMPASS_ORDER = tuple(name for name, _ in COMPASS)
# (largest perimeter spacing in m, class, typical error)
ACCURACY = (
    (1.3, "sub-metre", "about 0.3-0.8 m"),
    (2.5, "about 1-1.5 m", "about 1-1.5 m"),
    (3.5, "room level", "about 2-3 m"),
    (math.inf, "presence per zone at best", "coarser than about 3 m"),
)
ROOM_LEVEL = 2


class LayoutError(ValueError):
    """The layout file or an option is not usable; the message says why."""


@dataclass(frozen=True)
class Node:
    name: str
    role: str
    x: float
    y: float
    mac: str = ""


@dataclass(frozen=True)
class Wall:
    index: int
    a: tuple
    b: tuple
    db: float
    material: str = ""


@dataclass(frozen=True)
class Edge:
    index: int
    a: tuple
    b: tuple
    length: float
    u: tuple        # unit vector a -> b
    n: tuple        # unit outward normal
    compass: str    # the way the outward normal points


@dataclass
class Layout:
    source: str
    width_m: float
    depth_m: float
    nodes: list
    disabled: list
    ignored: list
    footprint: list
    footprint_given: bool
    zones: dict
    walls: list
    send_hz: float
    link_weights: dict


# --- geometry -------------------------------------------------------------------------------------------------------

def signed_area(poly):
    total = 0.0
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[(i + 1) % len(poly)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def seg_distance(px, py, ax, ay, bx, by):
    """Distance from (px, py) to the segment a-b."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def inside_polygon(x, y, poly, tol=1e-9):
    """True inside the polygon or on its boundary (even-odd rule)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if seg_distance(x, y, x1, y1, x2, y2) <= tol:
            return True
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def boundary_distance(x, y, poly):
    n = len(poly)
    return min(seg_distance(x, y, *poly[i], *poly[(i + 1) % n]) for i in range(n))


def signed_outside(x, y, poly):
    """Distance to the polygon's boundary: positive outside, negative inside, 0 on it."""
    d = boundary_distance(x, y, poly)
    return -d if d > 1e-9 and inside_polygon(x, y, poly) else d


def intersect_params(a, b, c, d):
    """(t, u) with a + t (b - a) = c + u (d - c), or None for parallel segments."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    return (qx * sy - qy * sx) / den, (qx * ry - qy * rx) / den


def crosses_wall(a, b, wall):
    """True when the link a-b passes through the wall strictly between its two ends."""
    p = intersect_params(a, b, wall.a, wall.b)
    return p is not None and 1e-9 < p[0] < 1 - 1e-9 and -1e-9 <= p[1] <= 1 + 1e-9


def outside_parts(a, b, poly, allowance_m=END_ALLOWANCE_M):
    """(raw, counted): the share of the segment a-b outside the polygon (the boundary counts as inside), and the
    same share without up to ``allowance_m`` of outside length at each end (boards stand outside on purpose)."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length < 1e-9:
        share = 0.0 if inside_polygon(a[0], a[1], poly) else 1.0
        return share, share
    cuts = [0.0, 1.0]
    for i in range(len(poly)):
        p = intersect_params(a, b, poly[i], poly[(i + 1) % len(poly)])
        if p is not None and -1e-9 <= p[0] <= 1 + 1e-9 and -1e-9 <= p[1] <= 1 + 1e-9:
            cuts.append(min(1.0, max(0.0, p[0])))
    cuts.sort()
    out, inside = 0.0, []
    for t0, t1 in zip(cuts, cuts[1:]):
        if t1 - t0 > 1e-12:
            tm = (t0 + t1) / 2
            if inside_polygon(a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1]), poly):
                inside.append((t0, t1))
            else:
                out += t1 - t0
    if not inside:
        return out, out
    allowed = min(inside[0][0] * length, allowance_m) + min((1.0 - inside[-1][1]) * length, allowance_m)
    return out, max(0.0, out - allowed / length)


def compass_of(nx, ny):
    return max(COMPASS, key=lambda item: nx * item[1][0] + ny * item[1][1])[0]


def footprint_edges(poly):
    sign = 1.0 if signed_area(poly) > 0 else -1.0
    edges = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        u = (dx / length, dy / length)
        n = (sign * u[1], -sign * u[0])
        edges.append(Edge(len(edges), a, b, length, u, n, compass_of(*n)))
    return edges


def on_edge(edge, x, y, poly):
    """True when a board at (x, y) sits on that edge's side of the footprint."""
    rx, ry = x - edge.a[0], y - edge.a[1]
    t = rx * edge.u[0] + ry * edge.u[1]
    s = rx * edge.n[0] + ry * edge.n[1]
    if not (-SIDE_IN_M <= s <= SIDE_OUT_M and -SIDE_MARGIN_M <= t <= edge.length + SIDE_MARGIN_M):
        return False
    if s < 0 or inside_polygon(x, y, poly):
        # On the inner side of the edge's line (or inside the footprint): only next to the wall itself.
        return seg_distance(x, y, *edge.a, *edge.b) <= SIDE_IN_M + 1e-9
    return True


def same_side_edge(a, b, edges, poly):
    """The footprint edge both boards sit on and the link runs along, or None."""
    dx, dy = b.x - a.x, b.y - a.y
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    min_cos = math.cos(math.radians(SAME_SIDE_ANGLE_DEG))
    for e in edges:
        if abs(dx * e.u[0] + dy * e.u[1]) / length >= min_cos and on_edge(e, a.x, a.y, poly) \
                and on_edge(e, b.x, b.y, poly):
            return e
    return None


def edge_name(edge, edges):
    if sum(1 for e in edges if e.compass == edge.compass) == 1:
        return f"{edge.compass} wall"
    return f"{edge.compass}-facing wall {_pt(edge.a)}-{_pt(edge.b)}"


def corner_name(first, second):
    if first == second:
        return first
    ns = [c for c in (first, second) if c in ("north", "south")]
    ew = [c for c in (first, second) if c in ("east", "west")]
    return f"{ns[0]}-{ew[0]}" if ns and ew else f"{first}-{second}"


def real_rate(send_hz):
    """Packets/s a sender really sends at CSI_SEND_HZ = send_hz: its sleep adds about 1 ms to every period."""
    return send_hz / (1.0 + 0.001 * send_hz)


def predict_rssi(length_m, wall_db=0.0, exponent=PATH_LOSS_EXPONENT):
    return REF_DBM - 10.0 * exponent * math.log10(max(length_m, MIN_DISTANCE_M) / REF_M) - wall_db


def _pt(p):
    return f"({p[0]:.2f}, {p[1]:.2f})"


def _join(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# --- layout ---------------------------------------------------------------------------------------------------------

def _number(value, what, minimum=None, positive=False):
    try:
        ok = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:       # an integer too large for a float
        ok = False
    if not ok:
        raise LayoutError(f"{what}: expected a number, got {value!r}")
    if positive and value <= 0:
        raise LayoutError(f"{what}: must be above 0, got {value!r}")
    if minimum is not None and value < minimum:
        raise LayoutError(f"{what}: must be at least {minimum}, got {value!r}")
    return float(value)


def _point(value, what):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise LayoutError(f"{what}: expected [x, y] in metres, got {value!r}")
    return (_number(value[0], what), _number(value[1], what))


def _polygon(value, what):
    if not isinstance(value, list) or len(value) < 3:
        raise LayoutError(f"{what}: expected a polygon [[x, y], ...] of at least 3 points, got {value!r}")
    pts = [_point(p, f"{what} point {i}") for i, p in enumerate(value)]
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if abs(signed_area(pts)) < 1e-9:
        raise LayoutError(f"{what}: the polygon has no area")
    return pts


def _zone(value, what):
    if isinstance(value, list) and len(value) == 4 and not any(isinstance(v, (list, tuple)) for v in value):
        x0, y0, x1, y1 = (_number(v, what) for v in value)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            raise LayoutError(f"{what}: the rectangle [x0, y0, x1, y1] has no area")
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return _polygon(value, what)


def load_layout(source):
    """A Layout from a JSON file path, or from an already parsed dict."""
    if isinstance(source, dict):
        config, name = source, "<layout>"
    else:
        path = Path(source)
        name = str(source)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise LayoutError(f"cannot read {path}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise LayoutError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(config, dict):
        raise LayoutError("the layout must be a JSON object")
    room = config.get("room")
    if not isinstance(room, dict):
        raise LayoutError('missing "room": {"width_m": ..., "depth_m": ...}')
    width = _number(room.get("width_m"), "room.width_m", positive=True)
    depth = _number(room.get("depth_m"), "room.depth_m", positive=True)
    nodes_cfg = config.get("nodes")
    if not isinstance(nodes_cfg, dict):
        raise LayoutError('missing "nodes": {name: {"role": ..., "x": ..., "y": ...}}')
    nodes, disabled, ignored = [], [], []
    for node_name, spec in sorted(nodes_cfg.items()):
        if not isinstance(spec, dict):
            raise LayoutError(f"node {node_name}: expected an object")
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise LayoutError(f"node {node_name}: enabled must be true or false, not {enabled!r}")
        if not enabled:
            disabled.append(node_name)
            continue
        role = spec.get("role")
        if role not in ("receiver", "sender"):
            ignored.append(node_name)
            continue
        nodes.append(Node(node_name, role, _number(spec.get("x"), f"node {node_name}: x"),
                          _number(spec.get("y"), f"node {node_name}: y"), str(spec.get("mac") or "")))
    if "footprint" in config:
        footprint, given = _polygon(config["footprint"], "footprint"), True
    else:
        footprint, given = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)], False
    zones_cfg = config.get("zones", {})
    if not isinstance(zones_cfg, dict):
        raise LayoutError('"zones" must map each name to [x0, y0, x1, y1] or a polygon [[x, y], ...]')
    zones = {str(k): _zone(v, f"zone {k}") for k, v in zones_cfg.items()}
    walls_cfg = config.get("walls", [])
    if not isinstance(walls_cfg, list):
        raise LayoutError('"walls" must be a list of {"a": [x, y], "b": [x, y], "db": ...}')
    walls = []
    for i, spec in enumerate(walls_cfg):
        if not isinstance(spec, dict):
            raise LayoutError(f"wall {i}: expected an object")
        a, b = _point(spec.get("a"), f"wall {i}: a"), _point(spec.get("b"), f"wall {i}: b")
        material = str(spec.get("material") or "")
        if "db" in spec:
            db = _number(spec["db"], f"wall {i}: db", minimum=0)
        elif material:
            key = material.lower().replace(" ", "_").replace("-", "_")
            if key not in MATERIAL_DB:
                raise LayoutError(f'wall {i}: unknown material {material!r}; give "db" or one of '
                                  f"{', '.join(MATERIAL_DB)}")
            db = MATERIAL_DB[key]
        else:
            raise LayoutError(f'wall {i}: give "db" (one-way loss in dB) or a "material"')
        walls.append(Wall(i, a, b, db, material))
    send_hz = _number(config.get("send_hz", SEND_HZ), "send_hz", positive=True)
    weights_cfg = config.get("link_weights", {})
    if not isinstance(weights_cfg, dict):
        raise LayoutError('"link_weights" must map link labels to numbers')
    weights = {str(k): _number(v, f"link_weights[{k}]") for k, v in weights_cfg.items()}
    return Layout(name, width, depth, nodes, disabled, ignored, footprint, given, zones, walls, send_hz, weights)


# --- coverage grid --------------------------------------------------------------------------------------------------

class Grid:
    """Square cells over the footprint's bounding box; the cells whose centre is inside the footprint count."""

    def __init__(self, poly, cell_m):
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        self.x0, self.y0, self.cell = min(xs), min(ys), cell_m
        self.cols = max(1, math.ceil((max(xs) - self.x0) / cell_m - 1e-9))
        self.rows = max(1, math.ceil((max(ys) - self.y0) / cell_m - 1e-9))
        if self.cols * self.rows > MAX_GRID_CELLS:
            raise LayoutError(f"--grid {cell_m} gives {self.cols} x {self.rows} cells; use a coarser grid")
        self.index = [[-1] * self.cols for _ in range(self.rows)]
        self.centres, self.colrow = [], []
        for r in range(self.rows):
            cy = self.y0 + (r + 0.5) * cell_m
            for c in range(self.cols):
                cx = self.x0 + (c + 0.5) * cell_m
                if inside_polygon(cx, cy, poly):
                    self.index[r][c] = len(self.centres)
                    self.centres.append((cx, cy))
                    self.colrow.append((c, r))

    @property
    def n(self):
        return len(self.centres)

    def band(self, a, b, half_width):
        """Indices of the cells whose centre lies within half_width of the segment a-b, between its ends."""
        ax, ay = a
        dx, dy = b[0] - ax, b[1] - ay
        l2 = dx * dx + dy * dy
        if l2 < 1e-12:
            return []
        limit = half_width * math.sqrt(l2) + 1e-9      # |cross product| <= half_width x length
        g = self.cell
        r_lo = max(0, math.floor((min(ay, b[1]) - half_width - self.y0) / g))
        r_hi = min(self.rows - 1, math.floor((max(ay, b[1]) + half_width - self.y0) / g))
        out = []
        for r in range(r_lo, r_hi + 1):
            row = self.index[r]
            py = self.y0 + (r + 0.5) * g - ay
            # Along this row both conditions are linear in px: solve for the x interval, then check each cell.
            lo, hi = -math.inf, math.inf
            if abs(dx) > 1e-12:
                p1, p2 = -py * dy / dx, (l2 - py * dy) / dx
                lo, hi = max(lo, min(p1, p2)), min(hi, max(p1, p2))
            elif not 0.0 <= py * dy <= l2:
                continue
            if abs(dy) > 1e-12:
                q1, q2 = (py * dx - limit) / dy, (py * dx + limit) / dy
                lo, hi = max(lo, min(q1, q2)), min(hi, max(q1, q2))
            elif abs(py * dx) > limit:
                continue
            if lo > hi:
                continue
            c_lo = 0 if lo == -math.inf else max(0, math.ceil((lo + ax - self.x0) / g - 0.5 - 1e-9))
            c_hi = self.cols - 1 if hi == math.inf else min(self.cols - 1,
                                                             math.floor((hi + ax - self.x0) / g - 0.5 + 1e-9))
            for c in range(c_lo, c_hi + 1):
                i = row[c]
                if i < 0:
                    continue
                px = self.x0 + (c + 0.5) * g - ax
                dot = px * dx + py * dy
                if 0.0 <= dot <= l2 and abs(px * dy - py * dx) <= limit:
                    out.append(i)
        return out


def _max_crossing(angles):
    """Largest angle between any two of these line directions (degrees in [0, 180)), from 0 to 90."""
    if len(angles) < 2:
        return 0.0
    s = sorted(a % 180.0 for a in angles)
    best = 0.0
    for a in s:
        # The partner farthest from a, as lines, is the one nearest to a + 90 (mod 180).
        j = bisect.bisect_left(s, (a + 90.0) % 180.0)
        for k in (j - 1, j % len(s)):
            d = abs(s[k] - a) % 180.0
            best = max(best, min(d, 180.0 - d))
    return best


def _counts(n, cell_lists, base=None, remove=()):
    counts = list(base) if base is not None else [0] * n
    for cells in remove:
        for i in cells:
            counts[i] -= 1
    for cells in cell_lists:
        for i in cells:
            counts[i] += 1
    return counts


def _score(counts, min_links):
    """(blind cells, -capped total): lower is better."""
    blind = total = 0
    cap = 2 * min_links
    for c in counts:
        if c < min_links:
            blind += 1
        total += c if c < cap else cap
    return blind, -total


def _blind_regions(grid, counts, min_links, edges):
    blind = {i for i, c in enumerate(counts) if c < min_links}
    seen, regions, g = set(), [], grid.cell
    for start in sorted(blind):
        if start in seen:
            continue
        seen.add(start)
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            c, r = grid.colrow[i]
            for nc, nr in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)):
                if 0 <= nc < grid.cols and 0 <= nr < grid.rows:
                    j = grid.index[nr][nc]
                    if j >= 0 and j in blind and j not in seen:
                        seen.add(j)
                        stack.append(j)
        xs = [grid.centres[i][0] for i in members]
        ys = [grid.centres[i][1] for i in members]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        nearest = min(edges, key=lambda e: seg_distance(cx, cy, *e.a, *e.b))
        if len(members) > 0.4 * grid.n:
            where = "across much of the footprint"
        elif seg_distance(cx, cy, *nearest.a, *nearest.b) <= 1.5:
            where = f"near the {nearest.compass} side"
        else:
            where = "in the interior"
        regions.append({"cells": len(members), "area_m2": round(len(members) * g * g, 2),
                        "x": [round(min(xs) - g / 2, 2), round(max(xs) + g / 2, 2)],
                        "y": [round(min(ys) - g / 2, 2), round(max(ys) + g / 2, 2)],
                        "centre": [round(cx, 2), round(cy, 2)], "fewest_links": min(counts[i] for i in members),
                        "nearest_side": nearest.compass, "where": where})
    regions.sort(key=lambda reg: (-reg["cells"], reg["centre"]))
    return regions


# --- analysis -------------------------------------------------------------------------------------------------------

def link_pairs(nodes, mode):
    """(receiver, sender) for every link of the mode."""
    if mode == "fixed":
        receivers = [n for n in nodes if n.role == "receiver"]
        senders = [n for n in nodes if n.role == "sender"]
        return [(rx, tx) for rx in receivers for tx in senders]
    return [(rx, tx) for rx in nodes for tx in nodes if rx.name != tx.name]


def dashboard_label(rx, tx, mode):
    """The dashboard's name for a fixed-mode link (receiver<-last octet of the sender's MAC)."""
    if mode == "fixed" and tx.mac:
        return f"{rx.name}<-{tx.mac[-2:]}"
    return f"{rx.name}<-{tx.name}"


def accuracy_class(perimeter_m, boards, sides_covered, blind_share):
    if boards == 0:
        return {"perimeter_m": round(perimeter_m, 2), "boards": 0, "spacing_m": None, "class": None,
                "typical_error": None, "reasons": ["no boards"], "boards_for": {}}
    spacing = perimeter_m / boards
    idx = next(i for i, (limit, _, _) in enumerate(ACCURACY) if spacing <= limit)
    reasons = []
    if blind_share > BLIND_DOWNGRADE_SHARE and idx < len(ACCURACY) - 1:
        idx += 1
        reasons.append(f"{blind_share:.0%} of the cells are blind: one class worse")
    if sides_covered < 3 and idx < ROOM_LEVEL:
        idx = ROOM_LEVEL
        reasons.append("boards on fewer than 3 sides: along the links, error is no better than a guess")
    if boards < MIN_BOARDS_FINE and idx < ROOM_LEVEL:
        idx = ROOM_LEVEL
        reasons.append(f"only {boards} boards: the literature needed about 10-20 for 1-2 m")
    return {"perimeter_m": round(perimeter_m, 2), "boards": boards, "spacing_m": round(spacing, 2),
            "class": ACCURACY[idx][1], "typical_error": ACCURACY[idx][2], "reasons": reasons,
            "boards_for": {"about 1-1.5 m": max(MIN_BOARDS_FINE, math.ceil(perimeter_m / 2.0 - 1e-9)),
                           "sub-metre": max(MIN_BOARDS_FINE, math.ceil(perimeter_m / 1.0 - 1e-9))}}


def _check_options(mode, grid_m, band_m, band_per_m, min_links, send_hz, offset_m, exponent):
    if mode not in ("fixed", "transceiver"):
        raise LayoutError(f"--mode must be fixed or transceiver, not {mode!r}")
    if not grid_m >= 0.05:
        raise LayoutError("--grid must be at least 0.05 m")
    if not (band_m >= 0 and band_per_m >= 0) or band_m + band_per_m <= 0:
        raise LayoutError("--band and --band-per-m must not be negative, and not both 0")
    if int(min_links) != min_links or min_links < 1:
        raise LayoutError("--min-links must be a whole number of at least 1")
    if send_hz is not None and not send_hz > 0:
        raise LayoutError("--send-hz must be above 0")
    if not offset_m >= 0.2:
        raise LayoutError("--offset must be at least 0.2 m")
    if not exponent > 0:
        raise LayoutError("--exponent must be above 0")


def analyse(layout, mode="fixed", grid_m=GRID_M, band_m=BAND_M, band_per_m=BAND_PER_M, min_links=MIN_LINKS,
            send_hz=None, offset_m=RING_OFFSET_M, exponent=PATH_LOSS_EXPONENT, search=True):
    """Everything about the layout as a JSON-ready dict (the --json output)."""
    _check_options(mode, grid_m, band_m, band_per_m, min_links, send_hz, offset_m, exponent)
    min_links = int(min_links)
    hz = layout.send_hz if send_hz is None else float(send_hz)
    rate = real_rate(hz)
    poly = layout.footprint
    edges = footprint_edges(poly)
    grid = Grid(poly, grid_m)
    nodes = layout.nodes

    def half_width(length):
        return band_m + band_per_m * length

    paths, links = {}, []
    for rx, tx in link_pairs(nodes, mode):
        a, b = (rx.x, rx.y), (tx.x, tx.y)
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        crossed = [w for w in layout.walls if crosses_wall(a, b, w)]
        wall_db = sum(w.db for w in crossed)
        rssi = predict_rssi(length, wall_db, exponent)
        raw_share, share = outside_parts(a, b, poly)
        same = same_side_edge(rx, tx, edges, poly)
        label = dashboard_label(rx, tx, mode)
        weight = layout.link_weights.get(label, 1.0) if mode == "fixed" else 1.0
        key = tuple(sorted((rx.name, tx.name)))
        if key not in paths:
            paths[key] = {"cells": grid.band(a, b, half_width(length)), "weight": weight,
                          "angle": math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0,
                          "half_width": half_width(length)}
        flags = []
        if same is not None:
            flags.append("SAME_SIDE")
        if share > OUTSIDE_SHARE:
            flags.append("OUTSIDE")
        if rssi < WEAK_DBM:
            flags.append("WEAK")
        if length < SHORT_M:
            flags.append("SHORT")
        links.append({"name": f"{rx.name}<-{tx.name}", "label": label, "receiver": rx.name, "sender": tx.name,
                      "length_m": round(length, 3), "rssi_dbm": round(rssi, 1),
                      "walls": [w.index for w in crossed], "wall_db": round(wall_db, 1),
                      "outside_share": round(share, 3), "outside_share_raw": round(raw_share, 3),
                      "band_half_width_m": round(half_width(length), 3),
                      "cells": len(paths[key]["cells"]), "weight": weight, "flags": flags,
                      "same_side_edge": same.index if same is not None else None})

    live = [p for p in paths.values() if p["weight"] > 0]
    counts = _counts(grid.n, [p["cells"] for p in live])
    angles = [[] for _ in range(grid.n)]
    for p in live:
        for i in p["cells"]:
            angles[i].append(p["angle"])
    blind_idx = [i for i, c in enumerate(counts) if c < min_links]
    good = sum(1 for i in range(grid.n) if counts[i] >= min_links and _max_crossing(angles[i]) >= CROSS_DEG)
    blind_share = len(blind_idx) / grid.n if grid.n else 1.0
    matrix = [[None] * grid.cols for _ in range(grid.rows)]
    for i, (c, r) in enumerate(grid.colrow):
        matrix[r][c] = counts[i]

    # Receivers: everyone who hears (in transceiver mode every board).
    receiving = [n for n in nodes if n.role == "receiver"] if mode == "fixed" else list(nodes)
    receivers = []
    for rx in receiving:
        mine = [lk for lk in links if lk["receiver"] == rx.name]
        load = len(mine) * rate
        row = {"name": rx.name, "senders": len(mine), "lines_per_s": round(load, 1), "rssi_max_dbm": None,
               "rssi_min_dbm": None, "strongest": None, "weakest": None, "spread_db": None, "warnings": []}
        if mine:
            strong = max(mine, key=lambda lk: lk["rssi_dbm"])
            weak = min(mine, key=lambda lk: lk["rssi_dbm"])
            spread = strong["rssi_dbm"] - weak["rssi_dbm"]
            row.update(rssi_max_dbm=strong["rssi_dbm"], rssi_min_dbm=weak["rssi_dbm"], strongest=strong["sender"],
                       weakest=weak["sender"], spread_db=round(spread, 1))
            if spread > SPREAD_WARN_DB:
                row["warnings"].append("RSSI_SPREAD")
        if load > LINE_CAP_PER_S:
            row["warnings"].append("SERIAL_OVER_CAP")
        elif load > LOAD_WARN_PER_S:
            row["warnings"].append("SERIAL_HIGH")
        receivers.append(row)

    # Zones.
    zones, zone_cells = [], {}
    for zname, zpoly in layout.zones.items():
        zcells = [i for i, (x, y) in enumerate(grid.centres) if inside_polygon(x, y, zpoly)]
        zone_cells[zname] = zcells
        zset = set(zcells)
        need = max(1, ZONE_SHARE * len(zcells))
        covering = [key for key, p in paths.items()
                    if p["weight"] > 0 and zcells and sum(1 for i in p["cells"] if i in zset) >= need]
        covering_set = set(covering)
        cover_links = [lk for lk in links if tuple(sorted((lk["receiver"], lk["sender"]))) in covering_set]
        zc = [counts[i] for i in zcells]
        zones.append({"name": zname, "polygon": [list(p) for p in zpoly], "cells": len(zcells),
                      "links": len(covering),
                      "link_names": sorted(_path_name(key, mode, links) for key in covering),
                      "min_per_cell": min(zc) if zc else None,
                      "mean_per_cell": round(sum(zc) / len(zc), 2) if zc else None,
                      "blind_cells": sum(1 for c in zc if c < min_links),
                      "receivers": sorted({lk["receiver"] for lk in cover_links}),
                      "senders": sorted({lk["sender"] for lk in cover_links}),
                      "max_crossing_deg": round(_max_crossing([paths[k]["angle"] for k in covering]), 1)})

    # Sides.
    node_rows = []
    covered_edges = set()
    for n in nodes:
        on = [e.index for e in edges if on_edge(e, n.x, n.y, poly)]
        covered_edges.update(on)
        node_rows.append({"name": n.name, "role": n.role, "x": n.x, "y": n.y,
                          "outside_m": round(signed_outside(n.x, n.y, poly), 3),
                          "sides": sorted({edges[i].compass for i in on}, key=COMPASS_ORDER.index)})
    present = [c for c in COMPASS_ORDER if any(e.compass == c for e in edges)]
    covered = [c for c in present if any(edges[i].compass == c for i in covered_edges)]
    sides = {"covered": covered, "missing": [c for c in present if c not in covered], "count": len(covered),
             "of": len(present), "edges_covered": len(covered_edges), "edges": len(edges)}

    perimeter = sum(e.length for e in edges)
    boards = len({(round(n.x, 3), round(n.y, 3)) for n in nodes})
    spacing = accuracy_class(perimeter, boards, len(covered), blind_share)

    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    result = {
        "layout": layout.source, "mode": mode,
        "parameters": {"grid_m": grid_m, "band_m": band_m, "band_per_m": band_per_m, "min_links": min_links,
                       "send_hz": hz, "real_rate_per_s": round(rate, 2), "ref_dbm": REF_DBM, "ref_m": REF_M,
                       "exponent": exponent, "weak_dbm": WEAK_DBM, "short_m": SHORT_M,
                       "outside_share": OUTSIDE_SHARE, "spread_warn_db": SPREAD_WARN_DB,
                       "line_cap_per_s": LINE_CAP_PER_S, "load_warn_per_s": LOAD_WARN_PER_S,
                       "offset_m": offset_m},
        "footprint": {"points": [list(p) for p in poly], "given": layout.footprint_given,
                      "area_m2": round(abs(signed_area(poly)), 2), "perimeter_m": round(perimeter, 2),
                      "bbox": [min(xs), min(ys), max(xs), max(ys)],
                      "edges": [{"index": e.index, "a": list(e.a), "b": list(e.b), "faces": e.compass,
                                 "length_m": round(e.length, 2)} for e in edges]},
        "nodes": node_rows, "disabled": layout.disabled, "ignored": layout.ignored,
        "walls": [{"index": w.index, "a": list(w.a), "b": list(w.b), "db": w.db, "material": w.material}
                  for w in layout.walls],
        "links": links, "paths": len(paths), "receivers": receivers,
        "coverage": {"cell_m": grid_m, "origin": [grid.x0, grid.y0], "cols": grid.cols, "rows": grid.rows,
                     "cells": grid.n, "counts": matrix, "blind": len(blind_idx),
                     "blind_share": round(blind_share, 4),
                     "well_conditioned_share": round(good / grid.n, 4) if grid.n else 0.0,
                     "blind_cells": [[round(grid.centres[i][0], 3), round(grid.centres[i][1], 3)]
                                     for i in blind_idx],
                     "blind_regions": _blind_regions(grid, counts, min_links, edges) if grid.n else []},
        "zones": zones, "sides": sides, "spacing": spacing,
    }
    ctx = SimpleNamespace(layout=layout, mode=mode, nodes=nodes, by_name={n.name: n for n in nodes}, edges=edges,
                          poly=poly, grid=grid, paths=paths, counts=counts, links=links, result=result,
                          min_links=min_links, rate=rate, hz=hz, offset=offset_m, half_width=half_width,
                          search=search, zone_cells=zone_cells)
    result["suggestions"] = _suggestions(ctx)
    return result


def _path_name(key, mode, links):
    if mode == "transceiver":
        return f"{key[0]}<->{key[1]}"
    for lk in links:
        if tuple(sorted((lk["receiver"], lk["sender"]))) == key:
            return lk["name"]
    return f"{key[0]}<->{key[1]}"


# --- suggestions ----------------------------------------------------------------------------------------------------

def ring_candidates(poly, edges, offset, nodes, step=None):
    """(x, y, side) positions on a ring ``offset`` outside the footprint, every ``step`` (default 0.5 m, coarser on big
    footprints), plus one off each convex corner; none within 0.5 m of a board."""
    if step is None:
        step = max(RING_STEP_M, sum(e.length for e in edges) / MAX_CANDIDATES)
    orient = 1.0 if signed_area(poly) > 0 else -1.0
    raw = []
    for e in edges:
        k = max(1, round(e.length / step))
        for j in range(k):
            t = (j + 0.5) * e.length / k
            raw.append((e.a[0] + e.u[0] * t + e.n[0] * offset, e.a[1] + e.u[1] * t + e.n[1] * offset, e.compass))
    for i, e2 in enumerate(edges):
        e1 = edges[i - 1]
        if (e1.u[0] * e2.u[1] - e1.u[1] * e2.u[0]) * orient > 1e-9:       # convex corner at e2.a
            bx, by = e1.n[0] + e2.n[0], e1.n[1] + e2.n[1]
            bl = math.hypot(bx, by)
            if bl > 1e-9:
                raw.append((e2.a[0] + bx / bl * offset, e2.a[1] + by / bl * offset,
                            corner_name(e1.compass, e2.compass)))
    keep = []
    for x, y, side in raw:
        if inside_polygon(x, y, poly) or boundary_distance(x, y, poly) < 0.6 * offset:
            continue
        if any(math.hypot(x - n.x, y - n.y) < 0.5 for n in nodes):
            continue
        if any(math.hypot(x - k[0], y - k[1]) < step / 2 for k in keep):
            continue
        keep.append((round(x, 2), round(y, 2), side))
    return keep


def _partners(ctx, role, pool=None):
    """The boards a new board of this role would link with."""
    pool = ctx.nodes if pool is None else pool
    if ctx.mode == "transceiver":
        return list(pool)
    return [n for n in pool if n.role == ("sender" if role == "receiver" else "receiver")]


def _roles(ctx):
    return ("receiver", "sender") if ctx.mode == "fixed" else ("board",)


def _new_cells(ctx, x, y, partners):
    return [ctx.grid.band((x, y), (p.x, p.y), ctx.half_width(math.hypot(p.x - x, p.y - y))) for p in partners]


def _plan_additions(ctx, candidates, steps=GREEDY_STEPS):
    """Up to ``steps`` boards added one after another, each where it leaves the fewest blind cells."""
    counts, pool, plan = list(ctx.counts), list(ctx.nodes), []
    for step in range(steps):
        before = _score(counts, ctx.min_links)
        if before[0] == 0:
            break
        best = {}
        for role in _roles(ctx):
            partners = _partners(ctx, role, pool)
            if not partners:
                continue
            for x, y, side in candidates:
                if any(math.hypot(x - p.x, y - p.y) < 0.5 for p in pool):
                    continue
                new = _new_cells(ctx, x, y, partners)
                score = _score(_counts(ctx.grid.n, new, base=counts), ctx.min_links)
                if role not in best or score < best[role][0]:
                    best[role] = (score, x, y, side, new)
        if not best:
            break
        role = min(best, key=lambda r: best[r][0])
        score, x, y, side, new = best[role]
        if score[0] >= before[0]:       # no fewer blind cells: a board here would not help enough to suggest
            break
        after = _counts(ctx.grid.n, new, base=counts)
        zones = []
        for zname, zcells in ctx.zone_cells.items():
            b0 = sum(1 for i in zcells if counts[i] < ctx.min_links)
            b1 = sum(1 for i in zcells if after[i] < ctx.min_links)
            if zcells and b0 - b1 >= max(4, 0.1 * len(zcells)):
                zones.append([zname, b0, b1])
        alternative = None
        for other, entry in best.items():
            if other != role and entry[0][0] < before[0]:
                alternative = {"role": other, "x": entry[1], "y": entry[2], "side": entry[3],
                               "blind_after": entry[0][0]}
        if ctx.mode == "fixed":
            load = (sum(1 for p in pool if p.role == "sender") + (role == "sender")) * ctx.rate
        else:
            load = len(pool) * ctx.rate
        plan.append({"step": step + 1, "role": role, "x": x, "y": y, "side": side, "links": len(new),
                     "blind_before": before[0], "blind_after": score[0], "zones": zones,
                     "alternative": alternative, "lines_per_s_after": round(load, 1)})
        counts = after
        pool.append(Node(f"new{step + 1}", "receiver" if role == "board" else role, x, y))
    return plan


def _best_for_zone(ctx, candidates, zname):
    """The board that adds the most links through the zone: (key, x, y, side, role, links, zone blind, score)."""
    zcells = ctx.zone_cells[zname]
    zset, need = set(zcells), max(1, ZONE_SHARE * len(zcells))
    best = None
    for role in _roles(ctx):
        partners = _partners(ctx, role)
        for x, y, side in candidates:
            new = _new_cells(ctx, x, y, partners)
            gain = sum(1 for cells in new if sum(1 for i in cells if i in zset) >= need)
            if gain == 0:
                continue
            after = _counts(ctx.grid.n, new, base=ctx.counts)
            zblind = sum(1 for i in zcells if after[i] < ctx.min_links)
            score = _score(after, ctx.min_links)
            key = (-gain, zblind, score)
            if best is None or key < best[0]:
                best = (key, x, y, side, role, gain, zblind, score)
    return best


def _best_move(ctx, node, avoid_edges, candidates):
    """Best place to move ``node`` (not onto the edges in avoid_edges): (score, x, y, side) or None."""
    partners = [p for p in _partners(ctx, node.role) if p.name != node.name]
    old = []
    for p in partners:
        path = ctx.paths.get(tuple(sorted((node.name, p.name))))
        if path is not None and path["weight"] > 0:
            old.append(path["cells"])
    base = _counts(ctx.grid.n, [], base=ctx.counts, remove=old)
    best = None
    for x, y, side in candidates:
        if any(on_edge(ctx.edges[i], x, y, ctx.poly) for i in avoid_edges):
            continue
        score = _score(_counts(ctx.grid.n, _new_cells(ctx, x, y, partners), base=base), ctx.min_links)
        if best is None or score < best[0]:
            best = (score, x, y, side)
    return best


def _names_with(items, limit=4):
    items = list(items)
    if len(items) <= limit:
        return _join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


def _suggestions(ctx):
    res = ctx.result
    out = []

    def add(priority, kind, text, **extra):
        out.append({"priority": priority, "kind": kind, "text": text, **extra})

    nodes, links, cov = ctx.nodes, ctx.links, res["coverage"]
    n_cells, blind = cov["cells"], cov["blind"]
    base_score = _score(ctx.counts, ctx.min_links)

    def impact(before, after):
        """Up to 20 priority points for removing blind cells, relative to all the blind cells there are now."""
        return 20.0 * (before - after) / max(1, blind)

    def effect(score):
        if score[0] < blind:
            return f"blind cells {blind} -> {score[0]}"
        return f"more links over thinly covered cells (blind cells stay at {blind})"

    if not nodes:
        add(100, "no_boards", "No enabled boards: add receivers and senders to the layout.")
        return _ranked(out)
    if not links:
        missing = "senders" if any(n.role == "receiver" for n in nodes) else "receivers"
        add(100, "no_links", f"No links: fixed mode needs receivers and senders; add {missing}, or plan the "
                             "round-robin with --mode transceiver.")
    candidates = ring_candidates(ctx.poly, ctx.edges, ctx.offset, nodes) if ctx.search and n_cells else []

    # Serial load.
    over = [r for r in res["receivers"] if "SERIAL_OVER_CAP" in r["warnings"]]
    high = [r for r in res["receivers"] if "SERIAL_HIGH" in r["warnings"]]
    for group, priority in ((over, 100), (high, 70)):
        if not group:
            continue
        worst = max(group, key=lambda r: r["lines_per_s"])
        verdict = (f"over the ~{LINE_CAP_PER_S:.0f} lines/s serial cap, so packets will be lost"
                   if priority == 100 else f"above the ~{LOAD_WARN_PER_S:.0f} lines/s to plan for "
                                           f"(cap ~{LINE_CAP_PER_S:.0f})")
        who = _names_with(r["name"] for r in group)
        text = (f"{who} {'hears' if len(group) == 1 else 'each hear up to'} {worst['senders']} senders x "
                f"{ctx.rate:.1f} packets/s = {worst['lines_per_s']:.0f} lines/s, {verdict}. ")
        if ctx.mode == "transceiver":
            text += ("A round-robin of this size needs compact per-link features computed on the boards (or "
                     "CSI_PRINT_STRIDE=4, lines about 3x shorter) or a lower rate per board.")
        else:
            at_min = worst["senders"] * real_rate(MIN_SEND_HZ)
            if ctx.hz > MIN_SEND_HZ and at_min <= LOAD_WARN_PER_S:
                text += f"Set CSI_SEND_HZ to {MIN_SEND_HZ:.0f} ({at_min:.0f} lines/s), or build the receivers with "
            else:
                text += "Build the receivers with "
            text += ("CSI_PRINT_STRIDE=4 (lines about 3x shorter; the gain is estimated, not yet measured on the "
                     "rig), or use fewer senders: every receiver hears every sender.")
        add(priority, "serial", text, receivers=[r["name"] for r in group])

    # Sides.
    sides = res["sides"]
    if sides["count"] < 3:
        missing = sides["missing"]
        add(90, "sides", f"Boards cover {sides['count']} of {sides['of']} sides"
                         f"{' (' + _join(sides['covered']) + ')' if sides['covered'] else ''}: add boards on the "
                         f"{_join(missing)} side{'s' if len(missing) > 1 else ''}. With boards on two sides only, "
                         "position along the links is no better than a guess (2.1 m against 3.3 m for a random "
                         "guess in a two-sided test).", missing=missing)
    elif sides["count"] < sides["of"]:
        add(58, "sides", f"Boards cover {sides['count']} of {sides['of']} sides: a board on the "
                         f"{_join(sides['missing'])} side would close the ring.", missing=sides["missing"])

    # Boards to add: a greedy plan, and one aimed at each zone with too few links.
    plan = _plan_additions(ctx, candidates) if candidates and blind else []
    for z in res["zones"]:
        if not z["cells"]:
            add(45, "zone", f"Zone {z['name']} has no grid cell inside the footprint: check its coordinates.",
                zone=z["name"])
            continue
        if z["links"] >= ctx.min_links:
            continue
        text = (f"Zone {z['name']} is covered by {_plural(z['links'], 'link')} ({ctx.min_links} or more are "
                f"needed to place someone in it)")
        extra = {}
        best = _best_for_zone(ctx, candidates, z["name"]) if candidates else None
        if best is not None:
            _, x, y, side, role, gain, zblind, _score_after = best
            text += (f": add a {role} at ({x:.2f}, {y:.2f}) on the {side} side, +{_plural(gain, 'link')} through "
                     f"it (blind cells in the zone {z['blind_cells']} -> {zblind})")
            if plan and (plan[0]["x"], plan[0]["y"], plan[0]["role"]) == (x, y, role):
                text += "; it is also the first board of the plan to add boards"
            extra = {"add": {"role": role, "x": x, "y": y, "side": side}}
        else:
            text += ": add boards on the sides facing it"
        add(85 - z["links"], "zone", text + ".", zone=z["name"], **extra)
    if plan:
        text = f"{blind} of {n_cells} cells ({blind / n_cells:.0%}) are blind (fewer than {ctx.min_links} links)."
        if cov["blind_regions"]:
            reg = cov["blind_regions"][0]
            text += (f" The largest blind patch is {reg['area_m2']:.1f} m2 around ({reg['centre'][0]:.1f}, "
                     f"{reg['centre'][1]:.1f}), {reg['where']}.")
        for step in plan:
            lead = " Add" if step["step"] == 1 else " Then"
            text += (f"{lead} a {step['role']} at ({step['x']:.2f}, {step['y']:.2f}) on the {step['side']} side: "
                     f"+{_plural(step['links'], 'link')}, blind cells {step['blind_before']} -> {step['blind_after']}")
            if step["zones"]:
                text += " (" + ", ".join(f"{name} {b0} -> {b1}" for name, b0, b1 in step["zones"]) + ")"
            text += "."
            load = step["lines_per_s_after"]
            if load > LOAD_WARN_PER_S and ctx.mode == "fixed" and step["role"] == "sender":
                text += (f" With it every receiver hears {load:.0f} lines/s (plan for {LOAD_WARN_PER_S:.0f}): build "
                         "them with CSI_PRINT_STRIDE=4")
                alt = step["alternative"]
                if alt is not None and alt["role"] == "receiver":
                    text += (f", or make it a receiver at ({alt['x']:.2f}, {alt['y']:.2f}) on the {alt['side']} "
                             f"side (blind cells -> {alt['blind_after']}; it needs its own USB port)")
                text += "."
        text += " Each board is placed on top of the ones before it."
        add(70 + impact(plan[0]["blind_before"], plan[0]["blind_after"]), "add_boards", text, plan=plan)

    # Same-side links: in fixed mode a wasted link, so suggest moving one board of the pair.
    same = [lk for lk in links if "SAME_SIDE" in lk["flags"]]
    if same and ctx.mode == "fixed":
        pending = {(lk["receiver"], lk["sender"]): lk for lk in same}
        while pending:
            tally = {}
            for rx, tx in pending:
                tally[rx] = tally.get(rx, 0) + 1
                tally[tx] = tally.get(tx, 0) + 1
            # The board with the most such links; on a tie a sender, since senders run from battery.
            name = min(tally, key=lambda nm: (-tally[nm], ctx.by_name[nm].role != "sender", nm))
            mine = [lk for k, lk in pending.items() if name in k]
            partners = sorted(lk["sender"] if lk["receiver"] == name else lk["receiver"] for lk in mine)
            edge_ids = sorted({lk["same_side_edge"] for lk in mine})
            walls = _join(edge_name(ctx.edges[i], ctx.edges) for i in edge_ids)
            cells = {i for lk in mine for i in ctx.paths[tuple(sorted((lk["receiver"], lk["sender"])))]["cells"]}
            one = len(mine) == 1
            reach = (f"cover{'s' if one else ''} none of the footprint" if not cells else
                     f"cover{'s' if one else ''} only a strip of {len(cells)} cells along it")
            if one:
                text = f"{name} and {partners[0]} share the {walls}: their link runs along the wall and {reach}."
            else:
                text = (f"{name} shares the {walls} with {_join(partners)}: those {len(mine)} links run along the "
                        f"wall and {reach}.")
            extra = {"node": name, "partners": partners}
            move = _best_move(ctx, ctx.by_name[name], edge_ids, candidates) if candidates else None
            priority = 62.0
            if move is not None and move[0] < base_score:
                score, x, y, side = move
                easy = " (senders run from battery, so they are the easier boards to move)" \
                    if ctx.by_name[name].role == "sender" else ""
                text += f" Move {name} to ({x:.2f}, {y:.2f}) on the {side} side{easy}: {effect(score)}."
                extra["move_to"] = {"x": x, "y": y, "side": side, "blind_after": score[0]}
                priority = 73 + impact(blind, score[0])          # moving costs nothing, adding costs a board
            elif ctx.search:
                text += f" No single move of {name} lowers the blind count; drop that pair when adding boards."
            add(priority, "same_side", text, **extra)
            for k in [k for k in pending if name in k]:
                del pending[k]
    elif same:
        paths_same = len({tuple(sorted((lk["receiver"], lk["sender"]))) for lk in same})
        add(20, "same_side", f"{paths_same} of {res['paths']} paths join boards on the same side. That is expected "
                             "with several boards per side and costs nothing in a round-robin, but those paths add "
                             "little.")

    # One forced gain per receiver.
    spread = [r for r in res["receivers"] if "RSSI_SPREAD" in r["warnings"]]
    if spread:
        worst = max(spread, key=lambda r: r["spread_db"])
        others = [r for r in spread if r is not worst]
        also = ("; " + ", ".join(f"{r['name']} {r['spread_db']:.1f} dB" for r in others[:5])
                + (" and more" if len(others) > 5 else "")) if others else ""
        add(60 + min(10.0, worst["spread_db"] - SPREAD_WARN_DB), "rssi_spread",
            f"{worst['name']} hears {worst['strongest']} at {worst['rssi_max_dbm']:.1f} dBm and {worst['weakest']} at "
            f"{worst['rssi_min_dbm']:.1f} dBm ({worst['spread_db']:.1f} dB apart{also}). A receiver forces one gain "
            f"for all its senders, so beyond about {SPREAD_WARN_DB:.0f} dB the near link clips and the far one "
            f"quantises: equalise the distances, or build {'it' if not others else 'these receivers'} with "
            "CSI_GAIN_LOCK_STRONGEST=1 (experimental, see firmware/README.md).",
            receivers=[r["name"] for r in spread])

    def link_list(flag, describe):
        # A same-side link is reported once, as same-side, even when it is also outside or short.
        chosen = [lk for lk in links if flag in lk["flags"] and (flag == "WEAK" or "SAME_SIDE" not in lk["flags"])]
        if ctx.mode == "transceiver":
            seen, unique = set(), []
            for lk in chosen:
                key = tuple(sorted((lk["receiver"], lk["sender"])))
                if key not in seen:
                    seen.add(key)
                    unique.append(lk)
            chosen = unique
        names = [f"{_path_name(tuple(sorted((lk['receiver'], lk['sender']))), ctx.mode, links)} ({describe(lk)})"
                 for lk in chosen]
        return chosen, _names_with(names)

    chosen, names = link_list("OUTSIDE", lambda lk: f"{lk['outside_share']:.0%}")
    if chosen:
        add(55, "outside", f"{names} {'runs' if len(chosen) == 1 else 'run'} largely outside the footprint, beyond "
                           f"the {END_ALLOWANCE_M:g} m allowed at each end (across a notch, around a corner or "
                           "along the outside of a wall): that part sees nothing inside. Pair boards whose line "
                           "crosses the building.", links=[lk["name"] for lk in chosen])
    chosen, names = link_list("WEAK", lambda lk: f"{lk['rssi_dbm']:.0f} dBm, {lk['length_m']:.1f} m"
                              + (f", {_plural(len(lk['walls']), 'wall')} {lk['wall_db']:.0f} dB" if lk["walls"]
                                 else ""))
    if chosen:
        add(50, "weak", f"{names} {'is' if len(chosen) == 1 else 'are'} predicted below {WEAK_DBM:.0f} dBm: expect "
                        "a noisy score. Give such links a 2-3 s window or a better (directional) antenna, or "
                        "shorten them and cross fewer walls.", links=[lk["name"] for lk in chosen])
    chosen, names = link_list("SHORT", lambda lk: f"{lk['length_m']:.2f} m")
    if chosen:
        add(35, "short", f"{names} {'is' if len(chosen) == 1 else 'are'} shorter than {SHORT_M:.1f} m: such a link "
                         "mostly senses the space around its two boards. Move them apart.",
            links=[lk["name"] for lk in chosen])

    # Board distance from the watched area.
    def where(row):
        d = row["outside_m"]
        if abs(d) < 0.005:
            return "on the edge"
        return f"inside by {-d:.2f} m" if d < 0 else f"{d:.2f} m out"

    def who(rows):
        descs = [where(r) for r in rows]
        if len(set(descs)) == 1:
            return f"{_names_with(r['name'] for r in rows)} ({descs[0]})"
        return _names_with(f"{r['name']} ({d})" for r, d in zip(rows, descs))

    near = [r for r in res["nodes"] if r["outside_m"] < NEAR_EDGE_M]
    if near:
        add(40, "board_distance", f"{who(near)}: closer than {NEAR_EDGE_M:.1f} m to the watched area, or inside it. "
                                  "Motion right next to a board, such as whoever placed it, moves all of its links "
                                  "at once and paints false heat into the map; about 0.75-1 m outside is better.",
            nodes=[r["name"] for r in near])
    far = [r for r in res["nodes"] if r["outside_m"] > FAR_OUT_M]
    if far:
        add(30, "board_distance", f"{who(far)}: more than {FAR_OUT_M:.1f} m outside the watched area, so their links "
                                  "are longer and weaker and see more of the outside. About 0.75-1 m outside is "
                                  "better.", nodes=[r["name"] for r in far])

    # Spacing and the accuracy to expect.
    sp = res["spacing"]
    if sp["class"] is not None and sp["class"] != ACCURACY[0][1]:
        need = sp["boards_for"]
        text = (f"Perimeter spacing {sp['spacing_m']:.2f} m ({sp['boards']} boards on {sp['perimeter_m']:.1f} m): "
                f"expect {sp['class']}.")
        if sp["class"] != ACCURACY[1][1] and need["about 1-1.5 m"] < need["sub-metre"]:
            text += (f" About 2 m spacing ({need['about 1-1.5 m']} boards) gives about 1-1.5 m, about 1 m "
                     f"({need['sub-metre']}) sub-metre.")
        else:
            text += f" Sub-metre needs about {need['sub-metre']} boards, about 1 m apart."
        add(30, "spacing", text)

    if not out:
        add(0, "ok", "No changes suggested: every link crosses the footprint, coverage has no blind cells and the "
                     "receivers are within their gain and serial budgets.")
    return _ranked(out)


def _ranked(out):
    out.sort(key=lambda s: -s["priority"])
    for i, s in enumerate(out, 1):
        s["rank"] = i
        s["priority"] = round(s["priority"], 1)
    return out


# --- text report ----------------------------------------------------------------------------------------------------

def _map_lines(res):
    cov = res["coverage"]
    g, (x0, y0), cols, rows, counts = cov["cell_m"], cov["origin"], cov["cols"], cov["rows"], cov["counts"]
    node_cells = {}
    for n in res["nodes"]:
        key = (math.floor((n["x"] - x0) / g), math.floor((n["y"] - y0) / g))
        node_cells[key] = "*" if key in node_cells else ("R" if n["role"] == "receiver" else "S")
    c_lo = min([0] + [k[0] for k in node_cells]) - 1
    c_hi = max([cols - 1] + [k[0] for k in node_cells]) + 1
    r_lo = min([0] + [k[1] for k in node_cells]) - 1
    r_hi = max([rows - 1] + [k[1] for k in node_cells]) + 1
    k = max(1, math.ceil((c_hi - c_lo + 1) / MAP_MAX_COLS))
    width = math.ceil((c_hi - c_lo + 1) / k)
    height = math.ceil((r_hi - r_lo + 1) / k)
    grid = []
    for br in range(height):
        line = []
        for bc in range(width):
            vals = []
            for r in range(r_lo + br * k, r_lo + br * k + k):
                for c in range(c_lo + bc * k, c_lo + bc * k + k):
                    if 0 <= r < rows and 0 <= c < cols and counts[r][c] is not None:
                        vals.append(counts[r][c])
            v = min(vals) if vals else None
            line.append("." if v is None else (str(v) if v < 10 else "+"))
        grid.append(line)
    for (c, r), ch in node_cells.items():
        bc, br = (c - c_lo) // k, (r - r_lo) // k
        if 0 <= bc < width and 0 <= br < height:
            prev = grid[br][bc]
            grid[br][bc] = "*" if prev in "RS*" else ch
    step = g * k
    xs0, ys0 = x0 + c_lo * g, y0 + r_lo * g
    ruler = [" "] * width
    last = -2
    for m in range(math.ceil(xs0 - 1e-9), math.floor(xs0 + width * step + 1e-9) + 1):
        j = round((m - xs0) / step)
        label = str(m)
        if j > last + 1 and j + len(label) <= width:
            ruler[j:j + len(label)] = list(label)
            last = j + len(label) - 1
    left = {}
    for m in range(math.ceil(ys0 - 1e-9), math.floor(ys0 + height * step + 1e-9) + 1):
        i = round((m - ys0) / step)
        if 0 <= i < height and i not in left:
            left[i] = str(m)
    lines = ["      " + "".join(ruler).rstrip()]
    for i, line in enumerate(grid):
        lines.append(f"{left.get(i, ''):>4}  " + "".join(line))
    scale = f"1 character = {step:.2f} m" + (" (each shows the fewest links among its cells)" if k > 1 else "")
    return lines, scale


def _wrap(text, indent):
    return textwrap.fill(text, WRAP, initial_indent=indent, subsequent_indent=" " * len(indent))


def render_text(res):
    p = res["parameters"]
    fp = res["footprint"]
    mode = res["mode"]
    lines = []
    receivers = [n for n in res["nodes"] if n["role"] == "receiver"]
    senders = [n for n in res["nodes"] if n["role"] == "sender"]
    if mode == "fixed":
        what = (f"fixed mode (today's firmware): {_plural(len(receivers), 'receiver')} x "
                f"{_plural(len(senders), 'sender')} = {_plural(len(res['links']), 'link')}")
    else:
        what = (f"transceiver mode (round-robin): {_plural(len(res['nodes']), 'board')} -> "
                f"{len(res['links'])} ordered links on {_plural(res['paths'], 'path')}")
    lines.append(f"Layout plan: {res['layout']}")
    lines.append(f"  {what}")
    x0, y0, x1, y1 = fp["bbox"]
    shape = "room rectangle" if not fp["given"] else f"{len(fp['points'])}-corner footprint"
    kinds = {}
    for w in res["walls"]:
        kind = f"{w['material'] or 'wall'} {w['db']:g} dB"
        kinds[kind] = kinds.get(kind, 0) + 1
    walls = _plural(len(res["walls"]), "wall")
    if kinds:
        walls += " (" + ", ".join(f"{n} x {kind}" for kind, n in kinds.items()) + ")"
    lines.append(f"  {shape} {x1 - x0:.2f} x {y1 - y0:.2f} m, {fp['area_m2']:.1f} m2, perimeter "
                 f"{fp['perimeter_m']:.1f} m; {_plural(len(res['zones']), 'zone')}; {walls}")
    lines.append(f"  senders at {p['send_hz']:g} Hz = {p['real_rate_per_s']:.1f} packets/s each")
    if res["disabled"]:
        lines.append(f"  disabled (left out): {', '.join(res['disabled'])}")
    if res["ignored"]:
        lines.append(f"  ignored (role not receiver or sender): {', '.join(res['ignored'])}")
    cov = res["coverage"]
    sp = res["spacing"]
    flagged = len({lk["name"] if mode == "fixed" else tuple(sorted((lk["receiver"], lk["sender"])))
                   for lk in res["links"] if lk["flags"]})
    unit = "links" if mode == "fixed" else "paths"
    lines.append(f"  Summary: {flagged} of {res['paths'] if mode != 'fixed' else len(res['links'])} {unit} flagged; "
                 f"{cov['blind_share']:.0%} of the cells blind; {res['sides']['count']} of {res['sides']['of']} "
                 f"sides covered; expected accuracy: {sp['class'] or 'none'}")
    lines.append(f"  Model: RSSI = {p['ref_dbm']:g} dBm at {p['ref_m']:g} m - {10 * p['exponent']:g} log10(d / "
                 f"{p['ref_m']:g} m) - walls; band half-width {p['band_m']:g} m + {p['band_per_m']:.0%} of the "
                 f"length; grid {p['grid_m']:g} m")
    lines.append("")

    # Links.
    rows = res["links"]
    if mode == "transceiver":
        seen, unique = set(), []
        for lk in rows:
            key = tuple(sorted((lk["receiver"], lk["sender"])))
            if key not in seen:
                seen.add(key)
                unique.append(dict(lk, name=f"{key[0]}<->{key[1]}"))
        rows = unique
    shown = rows if len(rows) <= MAX_TABLE_ROWS else [lk for lk in rows if lk["flags"]]
    title = "Links" if mode == "fixed" else "Paths (both directions alike)"
    lines.append(f"{title} ({len(rows)})")
    if rows:
        width = max(12, max(len(lk["name"]) for lk in rows) + 2)
        lines.append(f"  {'link':<{width}}{'length':>8}{'RSSI':>8}  {'walls':<12}{'outside':>8}{'cells':>7}  flags")
        for lk in shown:
            walls = f"{len(lk['walls'])} ({lk['wall_db']:g} dB)" if lk["walls"] else "-"
            extra = f"  weight {lk['weight']:g}" if lk["weight"] != 1.0 else ""
            lines.append(f"  {lk['name']:<{width}}{lk['length_m']:>6.2f} m{lk['rssi_dbm']:>8.1f}  {walls:<12}"
                         f"{lk['outside_share']:>8.0%}{lk['cells']:>7}  {' '.join(lk['flags']) or '-'}{extra}")
        if len(shown) < len(rows):
            lines.append(f"  ({len(rows) - len(shown)} unflagged not shown; --json lists every link)")
    else:
        lines.append("  none")
    lines.append(f"  RSSI in dBm, predicted; outside = share outside the footprint beyond {END_ALLOWANCE_M:g} m at "
                 "each end; cells = grid cells in the link's band")
    lines.append("")

    # Receivers.
    lines.append("Receivers" if mode == "fixed" else "Boards as receivers")
    for r in res["receivers"]:
        if r["senders"]:
            spread = f"RSSI {r['rssi_max_dbm']:.1f} to {r['rssi_min_dbm']:.1f} dBm, spread {r['spread_db']:.1f} dB"
            spread += " (over 6 dB: WARN)" if "RSSI_SPREAD" in r["warnings"] else " (ok)"
        else:
            spread = "no senders"
        load = "over the cap: WARN" if "SERIAL_OVER_CAP" in r["warnings"] else \
            "above 80: WARN" if "SERIAL_HIGH" in r["warnings"] else "ok"
        lines.append(f"  {r['name']:<8} {_plural(r['senders'], 'sender')}, {spread}; serial "
                     f"{r['lines_per_s']:.0f} lines/s of ~{p['line_cap_per_s']:.0f} ({load})")
    if not res["receivers"]:
        lines.append("  none")
    lines.append("")

    # Coverage.
    map_lines, scale = _map_lines(res)
    note = " (a path both directions share counts once)" if mode == "transceiver" else ""
    lines.append(f"Coverage: links per cell{note}")
    lines.append(f"  digits = links (+ = 10 or more; 0-{p['min_links'] - 1} blind), '.' outside the footprint, "
                 f"R receiver, S sender, * boards sharing a cell; north up, {scale}")
    lines.extend(map_lines)
    lines.append(f"  {cov['cells']} cells; blind (fewer than {p['min_links']} links): {cov['blind']} "
                 f"({cov['blind_share']:.0%}); well conditioned ({p['min_links']}+ links crossing at "
                 f"{CROSS_DEG:.0f} degrees or more): {cov['well_conditioned_share']:.0%}")
    for reg in cov["blind_regions"][:5]:
        lines.append(f"  blind patch: {reg['cells']} cells ({reg['area_m2']:.2f} m2), x {reg['x'][0]:.2f}-"
                     f"{reg['x'][1]:.2f}, y {reg['y'][0]:.2f}-{reg['y'][1]:.2f}, {reg['fewest_links']} at the worst, "
                     f"{reg['where']}")
    if len(cov["blind_regions"]) > 5:
        lines.append(f"  ... and {len(cov['blind_regions']) - 5} smaller blind patches")
    lines.append("")
    if res["zones"]:
        lines.append("Zones (links covering at least 25% of the zone's cells)")
        width = max(8, max(len(z["name"]) for z in res["zones"]) + 2)
        for z in res["zones"]:
            if not z["cells"]:
                lines.append(f"  {z['name']:<{width}} no cells inside the footprint")
                continue
            if mode == "fixed":
                diversity = f"{len(z['receivers'])} receivers, {len(z['senders'])} senders"
            else:
                diversity = f"{len(set(z['receivers']) | set(z['senders']))} boards"
            lines.append(f"  {z['name']:<{width}} {_plural(z['links'], 'link'):>9}, {diversity}, crossing up to "
                         f"{z['max_crossing_deg']:.0f} deg; per cell min {z['min_per_cell']}, mean "
                         f"{z['mean_per_cell']:.1f}; {z['blind_cells']}/{z['cells']} cells blind")
        lines.append("")
    sides = res["sides"]
    lines.append(f"Sides covered: {sides['count']} of {sides['of']}"
                 + (f" ({', '.join(sides['covered'])})" if sides["covered"] else "")
                 + (f"; missing {', '.join(sides['missing'])}" if sides["missing"] else "")
                 + f"; footprint edges with a board: {sides['edges_covered']} of {sides['edges']}")
    if sp["class"] is not None:
        lines.append(f"Expected accuracy: {sp['class']} ({sp['typical_error']}), from a perimeter spacing of "
                     f"{sp['spacing_m']:.2f} m ({sp['boards']} boards on {sp['perimeter_m']:.1f} m)")
        for reason in sp["reasons"]:
            lines.append(f"  {reason}")
        lines.append("  literature figures for one moving person with every board sending and receiving; "
                     "a guide, not a measurement")
    lines.append("")
    lines.append("Suggestions (most important first)")
    for s in res["suggestions"]:
        lines.append(_wrap(s["text"], f"  {s['rank']}. "))
    return "\n".join(lines)


# --- command line ---------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="plan_layout.py", description="Check a board layout: links, signal budget, serial load and coverage.",
        epilog="The module docstring (head of this file) explains the model and every flag.")
    parser.add_argument("layout", help="layout JSON: the room config schema plus optional footprint, zones, walls, "
                                       "send_hz")
    parser.add_argument("--mode", choices=("fixed", "transceiver"), default="fixed",
                        help="fixed: receivers hear senders (today's firmware, default); transceiver: every board "
                             "sends and receives (the planned round-robin)")
    parser.add_argument("--json", action="store_true", help="print everything as JSON instead of the report")
    parser.add_argument("--grid", type=float, default=GRID_M, help=f"coverage cell size, m (default {GRID_M})")
    parser.add_argument("--band", type=float, default=BAND_M,
                        help=f"sensitive band half-width of a zero-length link, m (default {BAND_M})")
    parser.add_argument("--band-per-m", type=float, default=BAND_PER_M,
                        help=f"band half-width added per metre of link length (default {BAND_PER_M})")
    parser.add_argument("--min-links", type=int, default=MIN_LINKS,
                        help=f"a cell covered by fewer links is blind (default {MIN_LINKS})")
    parser.add_argument("--send-hz", type=float, default=None,
                        help="the senders' nominal rate, Hz (default: the layout's send_hz, else 20)")
    parser.add_argument("--offset", type=float, default=RING_OFFSET_M,
                        help=f"how far outside the footprint suggested positions sit, m (default {RING_OFFSET_M})")
    parser.add_argument("--exponent", type=float, default=PATH_LOSS_EXPONENT,
                        help="path-loss exponent: 2 is 20 log10(d) (default); 3 for cluttered indoor paths")
    parser.add_argument("--no-search", action="store_true",
                        help="skip the search for positions to add or move boards")
    args = parser.parse_args(argv)
    try:
        layout = load_layout(args.layout)
        result = analyse(layout, mode=args.mode, grid_m=args.grid, band_m=args.band, band_per_m=args.band_per_m,
                         min_links=args.min_links, send_hz=args.send_hz, offset_m=args.offset,
                         exponent=args.exponent, search=not args.no_search)
    except LayoutError as exc:
        print(f"plan_layout.py: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
