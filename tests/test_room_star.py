"""A receiver outside the room rectangle: rx3 sits 1.5 m beyond the top wall (y = -1.5 in tests/fixtures/room_star.json).

The room's links, the simulator and the dashboard payload must all work with node positions outside
[0, width] x [0, depth]. The heatmap only covers the room rectangle, and that is where a person's footprint
is evaluated. The geometry figures in the comments are for the default simulated person (falloff 0.25 m).
"""
import urllib.request
import json
from pathlib import Path

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.detect import MOTION
from csi.heatmap import cell_centres, segment_falloff
from csi.links import LinkId, LinkMonitor, calibrate_links_report
from csi.room import Stand, load_room, link_responses
from csi.server import DashboardServer
from csi.simulate import simulate_room, simulate_room_links

ROOT = Path(__file__).resolve().parents[1]
STAR = ROOT / "tests" / "fixtures" / "room_star.json"
ROOM = load_room(STAR)
T0 = 1_700_000_000.0
LEAD_S = 12.0
RX3_A = "rx3<-00"    # rx3 (1.385, -1.5) to tx0 (2.77, 1.84)
RX3_B = "rx3<-01"    # rx3 (1.385, -1.5) to tx1 (0, 1.84)
RIGHT = "rx1<-00"    # rx1 (2.77, 0) to tx0: right wall
LEFT = "rx2<-01"     # rx2 (0, 0) to tx1: left wall
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01", "rx3<-00", "rx3<-01"}


def tagged(**kwargs):
    return list(simulate_room_links(room=ROOM, **kwargs))


@pytest.fixture(scope="module")
def thresholds():
    """Per-link thresholds from a simulated empty star room (margin 1.5)."""
    return calibrate_links_report(tagged(person=None, duration_s=25, seed=10), margin=1.5).thresholds


def states_by_label(packets, thresholds):
    monitor = LinkMonitor(thresholds)
    states: dict[str, list[tuple[float, str]]] = {}
    for receiver, pkt in packets:
        state = monitor.push(receiver, pkt)
        states.setdefault(LinkId(receiver, pkt.mac).label, []).append((pkt.host_ts, state))
    return states


def motion_fraction(states, label):
    # only the time the person is in the room (after the LEAD_S empty lead-in), past the first 1.5 s
    seq = [s for ts, s in states[label] if ts - T0 >= LEAD_S + 1.5]
    return sum(s == MOTION for s in seq) / len(seq)


def quiet_then(person, duration_s, seed=12):
    lead = tagged(person=None, duration_s=LEAD_S, seed=seed)
    stay = tagged(person=person, duration_s=duration_s, seed=seed, t0=T0 + LEAD_S)
    return lead + stay


def feed(state, packets, until):
    for receiver, pkt in packets:
        if pkt.host_ts >= until:
            break
        state.push(receiver, pkt)


def hot_cell_centre(payload):
    heat = payload["heatmap"]
    cells = np.array(heat["cells"])
    centres = cell_centres(heat["cols"], heat["rows"], ROOM.width_m, ROOM.depth_m)
    return centres[int(np.argmax(cells))]


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------- the config and the geometry

def test_star_fixture_puts_rx3_outside_the_room_and_keeps_six_links():
    rx3 = next(n for n in ROOM.receivers if n.name == "rx3")
    assert (rx3.x, rx3.y) == (1.385, -1.5)
    assert rx3.y < 0 and not (0 <= rx3.y <= ROOM.depth_m)     # outside the room rectangle
    assert [rx.name for rx in ROOM.receivers] == ["rx1", "rx2", "rx3"]
    assert {LinkId(rx.name, tx.mac).label for rx, tx in ROOM.links()} == LABELS
    assert len(ROOM.links()) == 6
    assert ROOM.link_weights == {"rx1<-01": 0.3}


def test_a_person_inside_the_room_responds_to_an_outside_receiver_through_the_segment():
    # rx3 is 1.5 m above the room; the link's segment still crosses the room, so the response is non-zero.
    responses = link_responses(ROOM, Stand(2.13, 0.3), np.arange(3.0))
    assert responses[("rx3", "1a:00:00:00:00:00")].min() > 0.9   # on the rx3 -> tx0 diagonal
    assert responses[("rx3", "1a:00:00:00:00:01")].max() < 0.01  # far from the rx3 -> tx1 diagonal


# ---------------------------------------------------------------- the payload over HTTP

def test_star_payload_over_http_lists_rx3_at_minus_1_5_and_six_links():
    sources = simulate_room(None, room=ROOM, duration_s=8.0, seed=21)
    server = DashboardServer(ROOM, sources, {}, port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
        payload = get_json(server.url + "/api/state")
    finally:
        server.stop()

    rx3 = next(n for n in payload["nodes"] if n["name"] == "rx3")
    assert (rx3["x"], rx3["y"], rx3["role"]) == (1.385, -1.5, "receiver")
    assert {row["id"] for row in payload["links"]} == LABELS
    assert len(payload["links"]) == 6
    weights = {row["id"]: row["weight"] for row in payload["links"]}
    assert weights["rx1<-01"] == 0.3 and weights["rx3<-00"] == 1.0
    # every link's endpoints are nodes of the payload, outside or not
    positions = {n["name"]: (n["x"], n["y"]) for n in payload["nodes"]}
    for row in payload["links"]:
        assert row["receiver"] in positions and row["sender"] in positions
    # the heatmap covers the room rectangle only
    assert payload["heatmap"]["extents"] == {"x_min": 0.0, "y_min": 0.0, "x_max": 2.77, "y_max": 1.84}


# ---------------------------------------------------------------- the room responds where the geometry says

def test_person_on_the_rx3_diagonal_inside_the_room_lights_rx3_a_and_puts_heat_on_it(thresholds):
    person = Stand(2.13, 0.3)   # on the rx3 -> tx0 diagonal, 0.3 m below the top wall
    packets = quiet_then(person, duration_s=12, seed=21)
    states = states_by_label(packets, thresholds)
    assert motion_fraction(states, RX3_A) > 0.8
    assert motion_fraction(states, RX3_B) < 0.05

    state = DashboardState(ROOM, thresholds)
    feed(state, packets, until=T0 + LEAD_S + 4.0)
    payload = state.snapshot(now=T0 + LEAD_S + 4.0)
    assert RX3_A in payload["heatmap"]["contributing"]
    assert RX3_B not in payload["heatmap"]["contributing"]
    hot = hot_cell_centre(payload)
    assert np.hypot(hot[0] - person.x, hot[1] - person.y) < 0.4


def test_top_middle_person_is_0_69_m_from_both_rx3_diagonals_so_their_motion_stays_low(thresholds):
    # (1.385, 0.3) is 0.69 m from each rx3 diagonal; with the simulator's 0.25 m falloff that is too far to
    # reach MOTION, so the rx3 links stay quiet here. This pins that limit of the model, not a hardware claim.
    states = states_by_label(quiet_then(Stand(1.385, 0.3), duration_s=12, seed=21), thresholds)
    assert motion_fraction(states, RX3_A) < 0.05
    assert motion_fraction(states, RX3_B) < 0.05


def test_person_near_the_bottom_left_corner_lights_the_links_that_pass_near_it(thresholds):
    # distances to the segments: rx1<-01 0.07 m, rx3<-01 0.14 m, rx2<-01 0.25 m; rx1<-00 2.5 m, rx3<-00 2.2 m
    states = states_by_label(quiet_then(Stand(0.25, ROOM.depth_m - 0.25), duration_s=12, seed=22), thresholds)
    for label in ("rx1<-01", "rx3<-01", "rx2<-01"):
        assert motion_fraction(states, label) > 0.8, label
    for label in ("rx1<-00", "rx3<-00"):
        assert motion_fraction(states, label) < 0.05, label


def test_person_near_the_bottom_left_corner_puts_heat_in_the_corner_from_the_links_that_pass_it(thresholds):
    corner = Stand(0.25, ROOM.depth_m - 0.25)
    packets = quiet_then(corner, duration_s=12, seed=22)
    state = DashboardState(ROOM, thresholds)
    feed(state, packets, until=T0 + LEAD_S + 4.0)
    payload = state.snapshot(now=T0 + LEAD_S + 4.0)
    contributing = set(payload["heatmap"]["contributing"])
    assert {"rx1<-01", "rx3<-01", "rx2<-01"} <= contributing     # the links within 0.25 m of the person
    assert not contributing & {"rx1<-00", "rx3<-00"}              # the links 2+ m away stay out
    hot = hot_cell_centre(payload)
    assert hot[0] < ROOM.width_m / 2 and hot[1] > ROOM.depth_m / 2  # hottest cell is in the bottom-left quarter


def test_bottom_centre_person_is_about_1_2_m_from_the_rx3_diagonals_and_no_footprint_reaches_them():
    # the rx3 diagonals pass 1.18 m from (1.385, 1.59); a 0.35 m falloff there is about 0.003
    centres = cell_centres(28, 18, ROOM.width_m, ROOM.depth_m)
    cell = int(np.argmin(np.hypot(centres[:, 0] - 1.385, centres[:, 1] - 1.59)))
    rx3_a = segment_falloff(centres, (1.385, -1.5), (2.77, 1.84), 0.35)
    rx1_b = segment_falloff(centres, (2.77, 0.0), (0.0, 1.84), 0.35)
    assert rx3_a[cell] < 0.01
    assert rx1_b[cell] > 0.2   # the diagonal that does cross the bottom-centre area carries the footprint
