"""The working area: fitted around the enabled nodes on the 10 m x 10 m plane, and refitted when a node moves.

``working_area`` and ``room_from_config`` are checked directly; the refit after a move through DashboardState (its
payload) and over HTTP (POST /api/node-position, GET /api/state), as in tests/test_node_position.py.
"""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.heatmap import Heatmap
from csi.links import room_link_ids
from csi.room import (AREA_MARGIN_M, AREA_MIN_M, PLANE_M, Node, load_room, move_node, room_config,
                      room_from_config, working_area)
from csi.server import DashboardServer
from csi.simulate import simulate_room, simulate_rounds
from tests.test_node_position import THRESHOLDS, get_state, hottest, post

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RIG4 = FIXTURES / "room_rig4.json"
TR8 = FIXTURES / "room_tr8.json"
T0 = 1_700_000_000.0


def nodes(*points):
    return [Node(f"n{i}", "transceiver", x, y) for i, (x, y) in enumerate(points)]


# ---------------------------------------------------------------- the area from the nodes

def test_the_area_is_the_nodes_bounding_box_padded_by_half_a_metre():
    assert (PLANE_M, AREA_MARGIN_M, AREA_MIN_M) == (10.0, 0.5, 1.0)
    assert working_area(nodes((2, 3), (5, 4))) == (1.5, 2.5, 4.0, 2.0)


def test_the_area_need_not_start_at_the_origin():
    assert working_area(nodes((6, 7), (8, 9), (7, 8))) == (5.5, 6.5, 3.0, 3.0)


def test_the_area_is_clamped_to_the_plane():
    assert working_area(nodes((0, 0), (10, 10))) == (0.0, 0.0, 10.0, 10.0)
    x0, y0, w, d = working_area(nodes((0.2, 9.9), (3, 9.6)))
    assert (x0, w) == (0.0, 3.5)                       # -0.3 clamped to 0
    assert (y0, y0 + d) == (9.0, 10.0)                 # 9.1 to 10.4, clamped to 10, then grown back to 1 m


def test_the_area_is_at_least_one_metre_a_side():
    assert working_area(nodes((5, 5))) == (4.5, 4.5, 1.0, 1.0)
    assert working_area(nodes((5, 5), (5.0, 5.0))) == (4.5, 4.5, 1.0, 1.0)
    assert working_area(nodes((0, 0))) == (0.0, 0.0, 1.0, 1.0)       # grown away from the plane's edge
    assert working_area(nodes((10, 10))) == (9.0, 9.0, 1.0, 1.0)


def test_a_room_file_with_a_size_still_loads_and_the_size_is_ignored():
    room = load_room(RIG4)                       # "room": {"width_m": 2.77, "depth_m": 1.84}, nodes at its corners
    assert room.area == (0.0, 0.0, 3.27, 2.34)
    config = json.loads(TR8.read_text(encoding="utf-8"))
    del config["room"]
    assert room_from_config(config) == load_room(TR8)          # no "room" block: the same room
    config["room"] = {"width_m": 99, "depth_m": 1}
    assert room_from_config(config) == load_room(TR8)


def test_a_node_off_the_plane_in_an_old_file_loads_outside_the_area():
    room = load_room(FIXTURES / "room_star.json")            # rx3 at (1.385, -1.5)
    assert room.y0_m == 0.0
    assert any(n.name == "rx3" and n.y == -1.5 for n in room.receivers)


def test_a_saved_room_config_has_no_size_and_loads_back_the_same():
    room = load_room(TR8)
    config = room_config(room)
    assert "room" not in config
    assert room_from_config(config) == room


# ---------------------------------------------------------------- the area follows a moved node

def test_moving_a_node_refits_the_area_and_the_grid_but_keeps_per_link_state():
    room = load_room(TR8)                                    # 4 x 3 m of nodes: area (0, 0) to (4.5, 3.5)
    state = DashboardState(room, mode="auto")
    for host_ts, data in simulate_rounds(room, duration_s=12.0, seed=2, t0=T0):
        state.push_broadcast(host_ts, data)
    before = state.snapshot()
    assert before["room"]["x0_m"] == 0.0 and before["room"]["width_m"] == 4.5

    state.move_node("n7", 0.0, 6.0)                          # bottom-left corner pulled 3 m down

    after = state.snapshot()
    assert (after["room"]["x0_m"], after["room"]["y0_m"], after["room"]["width_m"], after["room"]["depth_m"]) == \
        (0.0, 0.0, 4.5, 6.5)
    assert after["room"]["plane_m"] == 10.0
    assert after["heatmap"]["extents"] == {"x_min": 0.0, "y_min": 0.0, "x_max": 4.5, "y_max": 6.5}
    zones = after["room"]["zones"]
    assert max(z["y1"] for z in zones) == pytest.approx(6.5)               # the zones cover the new area
    assert len(zones) > len(before["room"]["zones"])
    keep = ("id", "state", "mode", "threshold", "baseline")
    assert [{k: r[k] for k in keep} for r in after["links"]] == [{k: r[k] for k in keep} for r in before["links"]]


def test_a_moved_area_away_from_the_origin_moves_the_heatmap_grid_with_it():
    room = move_node(move_node(load_room(RIG4), "rx2", 5.0, 5.0), "tx1", 5.0, 7.0)
    room = move_node(move_node(room, "rx1", 8.0, 5.0), "tx0", 8.0, 7.0)
    assert room.area == (4.5, 4.5, 4.0, 3.0)
    heat = Heatmap(room)
    assert heat.centres[:, 0].min() > 4.5 and heat.centres[:, 1].max() < 7.5
    labels = [lid.label for lid, _, _ in room_link_ids(room)]
    for t in np.arange(0.0, 12.5, 0.1):
        heat.advance(float(t), [{"id": label, "state": "EMPTY", "score": 3.0 if label == "rx2<-01" and t >= 12 else 1.0,
                                 "age_s": 0.0} for label in labels])
    x, y = hottest(heat)
    assert x == pytest.approx(5.0, abs=0.15) and 5.0 < y < 7.0     # along rx2 (5, 5) to tx1 (5, 7)


def test_a_simulated_walk_stays_inside_an_area_away_from_the_origin():
    from csi.server import default_walk
    room = move_node(move_node(load_room(RIG4), "rx2", 5.0, 5.0), "tx1", 5.0, 7.0)
    room = move_node(move_node(room, "rx1", 8.0, 5.0), "tx0", 8.0, 7.0)
    points = np.array(default_walk(room).waypoints)
    assert (points[:, 0] > 4.5).all() and (points[:, 0] < 8.5).all()
    assert (points[:, 1] > 4.5).all() and (points[:, 1] < 7.5).all()


# ---------------------------------------------------------------- over HTTP

@pytest.fixture
def server(tmp_path):
    path = tmp_path / "room.json"
    shutil.copyfile(RIG4, path)
    room = load_room(path)
    srv = DashboardServer(room, simulate_room(None, room=room, duration_s=2.0, seed=1), THRESHOLDS, port=0,
                          room_path=path)
    srv.start()
    assert srv.wait_finished(timeout=60)
    srv.room_path = path
    yield srv
    srv.stop()


@pytest.mark.parametrize("x, y", [(-0.01, 1.0), (10.01, 1.0), (1.0, -2.0), (1.0, 12.0)])
def test_a_position_off_the_plane_is_refused(server, x, y):
    original = server.room_path.read_bytes()
    status, body = post(server, {"node": "tx0", "x": x, "y": y})
    assert status == 400 and "off the plane" in body["error"]
    assert server.room_path.read_bytes() == original


def test_a_move_to_the_far_corner_of_the_plane_refits_the_area_and_saves_no_size(server):
    status, body = post(server, {"node": "tx0", "x": 10.0, "y": 10.0})

    assert status == 200
    assert body["area"] == {"x0_m": 0.0, "y0_m": 0.0, "width_m": 10.0, "depth_m": 10.0}
    room = get_state(server)["room"]
    assert (room["x0_m"], room["y0_m"], room["width_m"], room["depth_m"]) == (0.0, 0.0, 10.0, 10.0)
    saved = json.loads(server.room_path.read_text(encoding="utf-8"))
    assert saved["room"] == {"width_m": 2.77, "depth_m": 1.84}          # left as it was, not rewritten
    assert saved["nodes"]["tx0"]["x"] == 10.0
