"""Node layout comes from the room config: a third receiver (rx3 at top-middle), the per-node ``enabled``
flag, and the simulator and links_live deriving receivers, senders and MACs from the config.

The fixture tests/fixtures/room_5node.json is the committed rig plus rx3 at (1.385, 0). The committed
config/room.json keeps rx3 with ``"enabled": false``, so the committed behaviour is the 4-node one.
"""
import json
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.detect import MOTION
from csi.links import LinkId, LinkMonitor, calibrate_links_report
from csi.room import DEFAULT_ROOM_PATH, Stand, Walk, load_room, stand_near_wall
from csi.server import DashboardServer
from csi.simulate import DEFAULT_RECEIVERS, SENDER_MACS, Segment, simulate_link_streams, simulate_links, \
    simulate_room, simulate_room_links

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "room_5node.json"
ROOM5 = load_room(FIXTURE)
sys.path.insert(0, str(ROOT / "scripts"))
import links_live  # noqa: E402

T0 = 1_700_000_000.0
LEAD_S = 12.0  # an empty room first, so every link learns its quiet baseline before anyone arrives
RIGHT = "rx1<-00"    # rx1 (2.77, 0) to tx0 (2.77, 1.84): right wall
LEFT = "rx2<-01"     # rx2 (0, 0) to tx1 (0, 1.84): left wall
RX3_A = "rx3<-00"    # rx3 (1.385, 0) to tx0: new diagonal
RX3_B = "rx3<-01"    # rx3 (1.385, 0) to tx1: new diagonal
LABELS5 = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01", "rx3<-00", "rx3<-01"}
LABELS4 = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}


def mutated_config(tmp_path, mutate, name="room.json") -> Path:
    config = json.loads(FIXTURE.read_text())
    mutate(config)
    path = tmp_path / name
    path.write_text(json.dumps(config))
    return path


def disable_rx3(config):
    config["nodes"]["rx3"]["enabled"] = False


def tagged(room, **kwargs):
    return list(simulate_room_links(room=room, **kwargs))


@lru_cache(maxsize=None)
def calibrated5():
    """Per-link thresholds for the 5-node room from a simulated empty room (margin 1.5, as in test_simulate_room)."""
    return calibrate_links_report(tagged(ROOM5, person=None, duration_s=25, seed=10), margin=1.5).thresholds


def states_by_label(tagged_packets, thresholds):
    monitor = LinkMonitor(thresholds)
    states: dict[str, list[tuple[float, str]]] = {}
    for receiver, pkt in tagged_packets:
        state = monitor.push(receiver, pkt)
        states.setdefault(LinkId(receiver, pkt.mac).label, []).append((pkt.host_ts, state))
    return states


def motion_fraction(states, label, start_s=1.5, end_s=float("inf")):
    seq = [s for ts, s in states[label] if start_s <= ts - T0 <= end_s]
    return sum(s == MOTION for s in seq) / len(seq)


def quiet_then(room, person, duration_s, seed=12):
    """Per receiver: LEAD_S of an empty room, then ``person`` for ``duration_s``, on one host clock."""
    lead = tagged(room, person=None, duration_s=LEAD_S, seed=seed)
    stay = tagged(room, person=person, duration_s=duration_s, seed=seed, t0=T0 + LEAD_S)
    return lead + stay


def feed(state, packets, until=None):
    for receiver, pkt in packets:
        if until is not None and pkt.host_ts >= until:
            break
        state.push(receiver, pkt)


def grid(payload):
    heat = payload["heatmap"]
    return np.array(heat["cells"]).reshape(heat["rows"], heat["cols"])


def heat_centre_x(payload):
    cells = np.array(payload["heatmap"]["cells"])
    if cells.sum() <= 0:
        return None
    heat = payload["heatmap"]
    xs = (np.arange(heat["cols"]) + 0.5) * payload["room"]["width_m"] / heat["cols"]
    return float((grid(payload).sum(axis=0) * xs).sum() / cells.sum())


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


@contextmanager
def serving(room, sources, thresholds):
    server = DashboardServer(room, sources, thresholds, port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------- the fixture and the config loader

def test_fixture_has_three_receivers_and_two_senders_at_the_stated_positions():
    assert [rx.name for rx in ROOM5.receivers] == ["rx1", "rx2", "rx3"]
    assert [tx.name for tx in ROOM5.senders] == ["tx0", "tx1"]
    assert [tx.mac for tx in ROOM5.senders] == list(SENDER_MACS)
    rx3 = ROOM5.receivers[2]
    assert (rx3.x, rx3.y, rx3.role) == (1.385, 0.0, "receiver")
    assert {link.label for link in (LinkId(rx.name, tx.mac) for rx, tx in ROOM5.links())} == LABELS5


def test_frozen_rig_fixture_keeps_rx3_defined_but_disabled_so_the_default_room_stays_four_nodes():
    config = json.loads(DEFAULT_ROOM_PATH.read_text())
    assert config["nodes"]["rx3"] == {"role": "receiver", "chip": "ESP32-S3", "x": 1.385, "y": 0.0,
                                      "port_hint": "", "enabled": False}
    room = load_room()
    assert [rx.name for rx in room.receivers] == ["rx1", "rx2"]
    assert {LinkId(rx.name, tx.mac).label for rx, tx in room.links()} == LABELS4


def test_enabled_defaults_to_true_and_must_be_a_boolean(tmp_path):
    omitted = load_room(mutated_config(tmp_path, lambda c: c["nodes"]["rx3"].pop("enabled", None), "omitted.json"))
    assert [rx.name for rx in omitted.receivers] == ["rx1", "rx2", "rx3"]
    with pytest.raises(ValueError, match="enabled"):
        load_room(mutated_config(tmp_path, lambda c: c["nodes"]["rx3"].update(enabled="no"), "string.json"))


def test_a_disabled_node_is_not_a_receiver_or_sender_and_has_no_links(tmp_path):
    room = load_room(mutated_config(tmp_path, disable_rx3, "off.json"))
    assert [rx.name for rx in room.receivers] == ["rx1", "rx2"]
    assert {LinkId(rx.name, tx.mac).label for rx, tx in room.links()} == LABELS4


def test_a_disabled_sender_has_no_links_and_is_not_simulated(tmp_path):
    room = load_room(mutated_config(tmp_path, lambda c: c["nodes"]["tx1"].update(enabled=False), "tx1off.json"))
    assert [tx.name for tx in room.senders] == ["tx0"]
    assert {LinkId(rx.name, tx.mac).label for rx, tx in room.links()} == {"rx1<-00", "rx2<-00", "rx3<-00"}
    pairs = {(r, p.mac) for r, p in simulate_links(room=room, scenario=[Segment("empty", 1)], seed=1)}
    assert pairs == {(r, SENDER_MACS[0]) for r in ("rx1", "rx2", "rx3")}


# ---------------------------------------------------------------- simulator derives its layout from the room

def test_simulate_room_gives_one_stream_per_enabled_receiver_and_every_sender_mac_on_each():
    streams = simulate_room(None, room=ROOM5, duration_s=1.0, seed=1)
    assert set(streams) == {"rx1", "rx2", "rx3"}
    pairs = {(r, p.mac) for r, p in tagged(ROOM5, person=None, duration_s=2, seed=1)}
    assert pairs == {(r, m) for r in ("rx1", "rx2", "rx3") for m in SENDER_MACS}


def test_simulate_link_streams_and_links_follow_the_room_when_given_and_the_old_constants_otherwise():
    from_room = simulate_link_streams(room=ROOM5, scenario=[Segment("empty", 1)], seed=1)
    assert set(from_room) == {"rx1", "rx2", "rx3"}
    pairs = {(r, p.mac) for r, p in simulate_links(room=ROOM5, scenario=[Segment("empty", 1)], seed=1)}
    assert pairs == {(r, m) for r in ("rx1", "rx2", "rx3") for m in SENDER_MACS}

    legacy = simulate_link_streams(scenario=[Segment("empty", 1)], seed=1)
    assert set(legacy) == set(DEFAULT_RECEIVERS) == {"rx1", "rx2"}
    legacy_pairs = {(r, p.mac) for r, p in simulate_links(scenario=[Segment("empty", 1)], seed=1)}
    assert legacy_pairs == {(r, m) for r in ("rx1", "rx2") for m in SENDER_MACS}


def test_links_live_simulated_table_is_built_from_the_room(tmp_path):
    assert {lid.label for lid in links_live.simulated_links(ROOM5)} == LABELS5
    off = load_room(mutated_config(tmp_path, disable_rx3, "off.json"))
    assert {lid.label for lid in links_live.simulated_links(off)} == LABELS4


def test_links_live_command_line_draws_a_row_for_every_link_of_the_room_it_is_given(tmp_path):
    script = str(ROOT / "scripts" / "links_live.py")
    r = subprocess.run([sys.executable, script, "--simulate-links", "--room", str(FIXTURE), "--duration", "2"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert all(label in r.stdout for label in LABELS5)
    default = subprocess.run([sys.executable, script, "--simulate-links", "--duration", "2"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert default.returncode == 0, default.stderr
    assert "rx3" not in default.stdout and all(label in default.stdout for label in LABELS4)


# ---------------------------------------------------------------- the room responds where the geometry says

def test_person_at_top_middle_excites_the_rx3_links_and_not_the_wall_links():
    states = states_by_label(tagged(ROOM5, person=stand_near_wall(ROOM5, "top"), duration_s=12, seed=21),
                             calibrated5())
    assert motion_fraction(states, RX3_A) > 0.8
    assert motion_fraction(states, RX3_B) > 0.8
    assert motion_fraction(states, RIGHT) < 0.05
    assert motion_fraction(states, LEFT) < 0.05


def test_person_near_the_bottom_left_corner_excites_rx3_and_left_wall_more_than_the_right_wall():
    corner = Stand(0.25, ROOM5.depth_m - 0.25)
    states = states_by_label(tagged(ROOM5, person=corner, duration_s=12, seed=22), calibrated5())
    assert motion_fraction(states, RX3_B) > motion_fraction(states, RIGHT) + 0.5
    assert motion_fraction(states, LEFT) > motion_fraction(states, RIGHT) + 0.3


def test_person_walking_along_the_bottom_edge_moves_the_heat_centre_from_left_to_right():
    walk = Walk(((0.25, ROOM5.depth_m - 0.25), (ROOM5.width_m - 0.25, ROOM5.depth_m - 0.25)), speed_mps=0.5)
    packets = quiet_then(ROOM5, walk, duration_s=6.0)
    state = DashboardState(ROOM5, calibrated5())
    feed(state, packets, until=T0 + LEAD_S + 1.0)
    early = state.snapshot(now=T0 + LEAD_S + 1.0)
    feed(state, packets, until=T0 + LEAD_S + 4.0)
    late = state.snapshot(now=T0 + LEAD_S + 4.0)
    assert heat_centre_x(early) is not None and heat_centre_x(late) is not None
    assert heat_centre_x(late) - heat_centre_x(early) > 0.5
    assert heat_centre_x(early) < ROOM5.width_m / 2 < heat_centre_x(late)


# ---------------------------------------------------------------- the dashboard payload and HTTP interface

def test_dashboard_payload_lists_six_links_and_three_receiver_nodes_over_http():
    sources = simulate_room(stand_near_wall(ROOM5, "top"), room=ROOM5, duration_s=8.0, seed=21)
    with serving(ROOM5, sources, calibrated5()) as server:
        payload = get_json(server.url + "/api/state")
    assert {row["id"] for row in payload["links"]} == LABELS5
    assert len(payload["links"]) == 6
    receivers = [n["name"] for n in payload["nodes"] if n["role"] == "receiver"]
    senders = [n["name"] for n in payload["nodes"] if n["role"] == "sender"]
    assert receivers == ["rx1", "rx2", "rx3"] and senders == ["tx0", "tx1"]
    rx3 = next(n for n in payload["nodes"] if n["name"] == "rx3")
    assert (rx3["x"], rx3["y"]) == (1.385, 0.0)
    assert payload["status"]["receivers"] == {"rx1": "finished", "rx2": "finished", "rx3": "finished"}


def test_disabled_rx3_gives_no_links_no_payload_entry_and_no_stream(tmp_path):
    room = load_room(mutated_config(tmp_path, disable_rx3, "off.json"))
    assert set(simulate_room(None, room=room, duration_s=1.0, seed=1)) == {"rx1", "rx2"}

    # Even when a stream named rx3 is handed to the server (a port plugged in early), it is ignored.
    sources = simulate_room(stand_near_wall(ROOM5, "top"), room=ROOM5, duration_s=8.0, seed=21)
    with serving(room, sources, calibrated5()) as server:
        payload = get_json(server.url + "/api/state")
    assert {row["id"] for row in payload["links"]} == LABELS4
    assert all(row["receiver"] != "rx3" for row in payload["links"])
    assert {n["name"] for n in payload["nodes"]} == {"rx1", "rx2", "tx0", "tx1"}
    assert "rx3" not in payload["status"]["receivers"]

    state = DashboardState(room, {})
    feed(state, tagged(room, person=stand_near_wall(ROOM5, "top"), duration_s=3, seed=21))
    assert {row["id"] for row in state.snapshot()["links"]} == LABELS4


def test_committed_four_node_payload_is_identical_with_the_rx3_entry_present_or_absent(tmp_path):
    without = mutated_config(tmp_path, lambda c: c["nodes"].pop("rx3"), "without.json")
    payloads = []
    for room in (load_room(DEFAULT_ROOM_PATH), load_room(without)):
        state = DashboardState(room, {})
        feed(state, tagged(room, person=stand_near_wall(room, "right"), duration_s=8, seed=5))
        payloads.append(state.snapshot(now=T0 + 8))
    assert payloads[0] == payloads[1]
    assert {n["name"] for n in payloads[0]["nodes"]} == {"rx1", "rx2", "tx0", "tx1"}
    assert len(payloads[0]["links"]) == 4



def test_the_real_rig_config_loads_and_has_receivers_and_senders():
    real = Path(__file__).resolve().parent.parent / "config" / "room.json"
    room = load_room(real)
    assert len(room.receivers) >= 2 and len(room.senders) >= 2
    assert {rx.name for rx, _ in room.links()} == {rx.name for rx in room.receivers}
