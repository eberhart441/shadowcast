"""Moving a node on the dashboard map: POST /api/node-position, the geometry that follows it, and the room file.

Server tests run the dashboard on finished simulated streams and assert on the HTTP interface (/api/state and the
command's response) and on the room file the dashboard was started with (a copy of a fixture in tmp_path). The
geometry tests drive the heatmap and the transceiver room state directly, as tests/test_heatmap_graded.py does.
"""
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

import csi.room
from csi.dashboard import DashboardState
from csi.heatmap import Heatmap
from csi.links import room_link_ids
from csi.room import load_room, move_node, save_node_position
from csi.room_state import ZoneMap
from csi.server import DashboardServer
from csi.simulate import simulate_room, simulate_rounds

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RIG4 = FIXTURES / "room_rig4.json"
TR8 = FIXTURES / "room_tr8.json"
T0 = 1_700_000_000.0
THRESHOLDS = {"rx1<-00": 0.7, "rx1<-01": 0.8, "rx2<-00": 0.9, "rx2<-01": 1.1}
HTTP_TIMEOUT_S = 30.0


def post(server, body, path="/api/node-position", headers=None):
    req = urllib.request.Request(server.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def post_raw(server, data: bytes):
    req = urllib.request.Request(server.url + "/api/node-position", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def get_state(server):
    with urllib.request.urlopen(server.url + "/api/state", timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def nodes_of(payload):
    return {n["name"]: (n["x"], n["y"]) for n in payload["nodes"]}


@pytest.fixture
def room_file(tmp_path):
    path = tmp_path / "room.json"
    shutil.copyfile(RIG4, path)
    return path


@pytest.fixture
def serve():
    servers = []

    def start(room_path=None, room=None, **kwargs):
        room = room if room is not None else load_room(room_path)
        sources = simulate_room(None, room=room, duration_s=2.0, seed=1)
        server = DashboardServer(room, sources, THRESHOLDS, port=0, room_path=room_path, **kwargs)
        servers.append(server)
        server.start()
        assert server.wait_finished(timeout=60)
        return server

    yield start
    for server in servers:
        server.stop()


# ---------------------------------------------------------------- the command over HTTP

def test_moving_a_node_moves_it_in_the_payload_and_keeps_links_and_thresholds(serve, room_file):
    server = serve(room_file)
    before = get_state(server)

    status, body = post(server, {"node": "tx1", "x": 0.4, "y": 2.2})

    assert status == 200
    assert body == {"node": "tx1", "x": 0.4, "y": 2.2, "area": {"x0_m": 0.0, "y0_m": 0.0, "width_m": 3.27,
                                                               "depth_m": 2.7}, "saved": True, "path": str(room_file.resolve()),
                    "message": f"saved to {room_file.resolve()}"}
    after = get_state(server)
    assert nodes_of(after)["tx1"] == (0.4, 2.2)
    assert {k: v for k, v in nodes_of(after).items() if k != "tx1"} == \
        {k: v for k, v in nodes_of(before).items() if k != "tx1"}
    keep = ("id", "state", "threshold", "mode")
    assert [{k: row[k] for k in keep} for row in after["links"]] == [{k: row[k] for k in keep} for row in before["links"]]
    assert {row["id"]: row["threshold"] for row in after["links"]} == THRESHOLDS
    assert any("moved tx1 to (0.400, 2.200) m" in line for line in after["status"]["messages"])


def test_the_room_file_gets_the_new_position_rounded_and_keeps_everything_else(serve, room_file):
    original = json.loads(room_file.read_text(encoding="utf-8"))
    server = serve(room_file)

    status, body = post(server, {"node": "rx2", "x": 0.123456, "y": 0.5})

    assert status == 200 and (body["x"], body["y"]) == (0.123, 0.5)
    text = room_file.read_text(encoding="utf-8")
    saved = json.loads(text)
    expected = json.loads(json.dumps(original))
    expected["nodes"]["rx2"]["x"], expected["nodes"]["rx2"]["y"] = 0.123, 0.5
    assert saved == expected
    assert list(saved) == list(original) and list(saved["nodes"]) == list(original["nodes"])
    assert list(saved["nodes"]["rx2"]) == list(original["nodes"]["rx2"])        # key order kept
    assert text == json.dumps(expected, indent=2) + "\n"                        # the 2-space layout kept
    assert sorted(p.name for p in room_file.parent.iterdir()) == ["room.json"]  # no temporary file left
    assert load_room(room_file).receivers[1].x == 0.123                         # and the next start reads it


def test_a_failed_write_leaves_the_file_and_the_map_unchanged(serve, room_file, monkeypatch):
    server = serve(room_file)
    original = room_file.read_bytes()

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(csi.room.os, "replace", refuse)
    status, body = post(server, {"node": "tx0", "x": 1.0, "y": 1.0})

    assert status == 500 and "nothing was moved" in body["error"]
    assert room_file.read_bytes() == original
    assert sorted(p.name for p in room_file.parent.iterdir()) == ["room.json"]
    assert nodes_of(get_state(server))["tx0"] == (2.77, 1.84)


def test_without_a_room_file_the_move_applies_in_memory_and_says_nothing_was_saved(serve):
    server = serve(room=load_room(RIG4))

    status, body = post(server, {"node": "tx0", "x": 2.0, "y": 1.5})

    assert status == 200
    assert body["saved"] is False and body["path"] is None and "nothing was saved" in body["message"]
    assert nodes_of(get_state(server))["tx0"] == (2.0, 1.5)


@pytest.mark.parametrize("body, needle", [
    ({"node": "nope", "x": 1.0, "y": 1.0}, "unknown node"),
    ({"node": "rx3", "x": 1.0, "y": 1.0}, "disabled"),
    ({"node": "tx0", "x": "1.0", "y": 1.0}, "x must be a finite number"),
    ({"node": "tx0", "x": 1.0, "y": None}, "y must be a finite number"),
    ({"node": "tx0", "x": True, "y": 1.0}, "x must be a finite number"),
    ({"node": "tx0", "x": 1.0}, "y must be a finite number"),
    ({"node": "tx0", "x": 100.0, "y": 1.0}, "off the plane"),
    ({"node": 3, "x": 1.0, "y": 1.0}, "name the node"),
    ({"node": "tx0", "x": 1.0, "y": 1.0, "z": 0}, "unknown field"),
])
def test_bad_input_is_refused_and_nothing_changes(serve, room_file, body, needle):
    server = serve(room_file)
    original = room_file.read_bytes()

    status, reply = post(server, body)

    assert status == 400 and needle in reply["error"]
    assert room_file.read_bytes() == original
    assert nodes_of(get_state(server))["tx0"] == (2.77, 1.84)


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_a_non_finite_position_is_refused(serve, room_file, literal):
    server = serve(room_file)

    status, reply = post_raw(server, b'{"node": "tx0", "x": ' + literal + b', "y": 1.0}')

    assert status == 400 and "finite" in reply["error"]
    assert nodes_of(get_state(server))["tx0"] == (2.77, 1.84)


def test_the_command_is_refused_with_controls_off_and_in_replay(serve, room_file):
    original = room_file.read_bytes()
    open_bind = serve(room_file, host="0.0.0.0")
    replay = serve(room_file, replay=True)

    status, body = post(open_bind, {"node": "tx0", "x": 1.0, "y": 1.0})
    assert status == 403 and "control is disabled" in body["error"]
    status, body = post(replay, {"node": "tx0", "x": 1.0, "y": 1.0})
    assert status == 409 and "replay" in body["error"]
    assert room_file.read_bytes() == original


# ---------------------------------------------------------------- the geometry follows the node

def _feed(heat, t0, t1, labels, hot=(), step=0.1):
    for t in np.arange(t0, t1, step):
        heat.advance(float(t), [{"id": label, "state": "EMPTY", "score": 3.0 if label in hot else 1.0, "age_s": 0.0}
                                for label in labels])


def hottest(heat):
    """Centre of the cells within 1 % of the hottest one, (x, y) metres."""
    hot = heat.target >= 0.99 * heat.target.max()
    return tuple(heat.centres[hot].mean(axis=0))


def test_the_footprint_heatmap_follows_a_moved_node_and_keeps_its_baselines():
    room = load_room(RIG4)
    labels = [lid.label for lid, _, _ in room_link_ids(room)]
    heat = Heatmap(room)
    _feed(heat, 0.0, 12.0, labels)                      # baselines warm up at 1.0
    baselines = dict(heat.baselines)
    _feed(heat, 12.0, 12.5, labels, hot=("rx2<-01",))      # rx2 (0, 0) to tx1 (0, 1.84): the left wall
    hot_before = hottest(heat)

    heat.set_room(move_node(room, "tx1", 2.77, 0.0))   # the link now runs along the top wall
    _feed(heat, 12.5, 13.0, labels, hot=("rx2<-01",))

    hot_after = hottest(heat)
    assert hot_before == pytest.approx((0.05, 0.92), abs=0.1)     # along the left wall
    assert hot_after == pytest.approx((1.385, 0.05), abs=0.1)     # along the top wall
    assert heat.baselines == pytest.approx(baselines)      # quiet baselines kept (score 1.0 throughout)


def test_the_rti_heatmap_follows_a_moved_transceiver():
    room = load_room(TR8)
    labels = [lid.label for lid, _, _ in room_link_ids(room)]
    heat = Heatmap(room)
    assert heat.imaging == "rti"
    _feed(heat, 0.0, 12.0, labels)
    hot = ("n1<-n3", "n3<-n1")                          # n1 (0, 0) to n3 (4, 0): the top wall
    _feed(heat, 12.0, 12.5, labels, hot=hot)
    x_before, y_before = hottest(heat)

    heat.set_room(move_node(room, "n3", 0.5, 3.0))     # now n1 to (0.5, 3): down the left side
    _feed(heat, 12.5, 13.0, labels, hot=hot)

    x_after, y_after = hottest(heat)
    assert y_before < 0.6 and x_before > 1.0
    assert x_after < 0.8 and y_after > 0.8


def test_the_rti_heatmap_and_the_zone_coverage_follow_a_moved_transceiver():
    room = load_room(TR8)
    state = DashboardState(room, mode="auto")
    for host_ts, data in simulate_rounds(room, duration_s=3.0, seed=2, t0=T0):
        state.push_broadcast(host_ts, data)
    before = state.snapshot()
    cover_before = state._monitor.room_zones.map.cover.copy()

    moved = state.move_node("n2", 2.0, 1.5)                  # the top-middle node to the room centre

    after = state.snapshot()
    assert (moved.x, moved.y) == (2.0, 1.5)
    assert nodes_of(after)["n2"] == (2.0, 1.5)
    labels = [(label, rx, tx) for label, rx, tx in
              ((f"{rx.name}<-{tx.name}", rx, tx) for rx, tx in state.room.transceiver_links())]
    expected = ZoneMap(state.room, labels).cover
    assert np.array_equal(state._monitor.room_zones.map.cover, expected)
    assert not np.array_equal(cover_before, expected)
    assert [(r["id"], r["state"], r["mode"]) for r in after["links"]] == \
        [(r["id"], r["state"], r["mode"]) for r in before["links"]]
    assert after["status"]["room"]["zones"].keys() == before["status"]["room"]["zones"].keys()


def test_a_crlf_room_file_keeps_its_line_endings(tmp_path):
    path = tmp_path / "room.json"
    path.write_bytes(RIG4.read_text(encoding="utf-8").replace("\n", "\r\n").encode())

    save_node_position(path, "tx0", 1.5, 1.0)

    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert json.loads(raw)["nodes"]["tx0"]["x"] == 1.5
    assert sorted(os.listdir(tmp_path)) == ["room.json"]
