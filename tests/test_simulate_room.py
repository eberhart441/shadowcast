"""Simulated person in the room from the committed room config, driven through the text seam into LinkMonitor."""
import json
import time
from functools import lru_cache

import pytest

from csi.detect import EMPTY, MOTION
from csi.links import NO_SIGNAL, LinkId, LinkMonitor, calibrate_links_report
from csi.room import DEFAULT_ROOM_PATH, Walk, load_room, stand_at_centre, stand_near_wall
from csi.simulate import simulate_room, simulate_room_links

T0 = 1_700_000_000.0
ROOM = load_room()
LEFT = "rx2<-01"    # rx2 (0, 0) to tx1 (0, 1.84): the left wall
RIGHT = "rx1<-00"   # rx1 (2.77, 0) to tx0 (2.77, 1.84): the right wall
DIAG_A = "rx2<-00"  # rx2 (0, 0) to tx0 (2.77, 1.84)
DIAG_B = "rx1<-01"  # rx1 (2.77, 0) to tx1 (0, 1.84)
LABELS = {LEFT, RIGHT, DIAG_A, DIAG_B}


def tagged(**kwargs):
    return list(simulate_room_links(**kwargs))


def label_of(receiver, pkt):
    return LinkId(receiver, pkt.mac).label


def run(tagged_packets, thresholds):
    """Push every packet through LinkMonitor. Returns (monitor, {label: [(host_ts, state), ...]})."""
    monitor = LinkMonitor(thresholds)
    states: dict[str, list[tuple[float, str]]] = {}
    for receiver, pkt in tagged_packets:
        state = monitor.push(receiver, pkt)
        states.setdefault(label_of(receiver, pkt), []).append((pkt.host_ts, state))
    return monitor, states


@lru_cache(maxsize=None)
def _calibrated_committed_room():
    # margin 1.5 (not the default 1.0): at 1.0 the held-out empty room crosses the single-clip p99 (see test_calibration_margin.py)
    return calibrate_links_report(tagged(person=None, duration_s=25, seed=10), margin=1.5).thresholds


def calibrated(config=None):
    """Thresholds per link from an empty room, the calibration the monitor would be given."""
    if config is None:
        return _calibrated_committed_room()  # the same empty room for every test: simulated once per process
    return calibrate_links_report(tagged(person=None, duration_s=25, seed=10, room=config), margin=1.5).thresholds


def motion_fraction(states, label, start_s=1.5, end_s=float("inf")):
    """Share of MOTION states for one link, counting from ``start_s`` after the first packet (scorer warm-up)."""
    seq = [s for ts, s in states[label] if start_s <= ts - T0 <= end_s]
    return sum(s == MOTION for s in seq) / len(seq)


@pytest.mark.slow
def test_empty_room_keeps_every_link_quiet():
    cal = calibrated()
    assert set(cal) == LABELS
    _, states = run(tagged(person=None, duration_s=25, seed=11), cal)
    assert set(states) == LABELS
    for label in LABELS:
        assert MOTION not in {s for _, s in states[label]}, label
        assert EMPTY in {s for _, s in states[label]}, label


@pytest.mark.parametrize("side, near, far", [("left", LEFT, RIGHT), ("right", RIGHT, LEFT)])
def test_person_next_to_a_wall_excites_that_wall_link_far_more_than_the_opposite(side, near, far):
    _, states = run(tagged(person=stand_near_wall(ROOM, side), duration_s=12, seed=12), calibrated())
    assert motion_fraction(states, near) > 0.8
    assert motion_fraction(states, far) < 0.05


def test_person_at_centre_excites_both_diagonals_about_equally_and_no_wall_link():
    _, states = run(tagged(person=stand_at_centre(ROOM), duration_s=12, seed=13), calibrated())
    a, b = motion_fraction(states, DIAG_A), motion_fraction(states, DIAG_B)
    assert a > 0.8 and b > 0.8
    assert abs(a - b) < 0.1
    assert motion_fraction(states, LEFT) < 0.05
    assert motion_fraction(states, RIGHT) < 0.05


def test_walk_stops_at_the_far_wall_when_not_looping():
    walk = Walk(waypoints=((0.25, 0.92), (2.52, 0.92)), speed_mps=0.5)
    _, states = run(tagged(person=walk, duration_s=14, seed=14), calibrated())
    # Walk takes about 4.5 s; after that the person stands by the right wall.
    assert motion_fraction(states, RIGHT, start_s=6.0) > 0.8
    assert motion_fraction(states, LEFT, start_s=6.0) < 0.05


def test_walk_that_loops_comes_back_to_the_left_wall():
    walk = Walk(waypoints=((0.25, 0.92), (2.52, 0.92)), speed_mps=0.5, loop=True)
    _, states = run(tagged(person=walk, duration_s=14, seed=15), calibrated())
    # Round trip is about 9.1 s: the person is back at the left wall around 9 s (scorer lags about 1 s).
    assert motion_fraction(states, LEFT, start_s=9.0, end_s=10.0) > 0.8


def test_link_that_drops_out_sends_nothing_afterwards_and_reports_no_signal():
    cal = calibrated()
    packets = tagged(person=stand_near_wall(ROOM, "left"), duration_s=8, seed=16,
                     drop_after={LEFT: 3.0})
    dropped = [(r, p) for r, p in packets if label_of(r, p) == LEFT]
    assert any(p.host_ts < T0 + 3.0 for _, p in dropped)
    assert all(p.host_ts < T0 + 3.0 for _, p in dropped)
    monitor, _ = run(packets, cal)
    rows = {row["id"]: row for row in monitor.snapshot(now=packets[-1][1].host_ts)}
    assert rows[LEFT]["state"] == NO_SIGNAL
    assert rows[RIGHT]["state"] != NO_SIGNAL


@pytest.mark.slow
def test_moving_a_sender_in_the_config_moves_which_link_responds(tmp_path):
    config = json.loads(DEFAULT_ROOM_PATH.read_text())
    config["nodes"]["tx1"]["x"], config["nodes"]["tx1"]["y"] = 2.77, 1.84  # tx1 moved to the far corner
    moved_path = tmp_path / "room.json"
    moved_path.write_text(json.dumps(config))
    moved = load_room(moved_path)

    person = stand_near_wall(ROOM, "left")
    _, original = run(tagged(person=person, duration_s=12, seed=17), calibrated())
    _, after_move = run(tagged(person=person, duration_s=12, seed=17, room=moved), calibrated(moved))
    assert motion_fraction(original, LEFT) > 0.8
    assert motion_fraction(after_move, LEFT) < 0.05


def test_realtime_paces_the_lines_to_the_wall_clock():
    start = time.time()
    streams = simulate_room(person=None, duration_s=0.5, realtime=True)
    lines = list(streams["rx1"])
    elapsed = time.time() - start
    assert len(lines) > 50
    assert 0.4 <= elapsed < 3.0
