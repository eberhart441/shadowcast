"""Motion track (csi/tracker.py): a Kalman-smoothed dot following the heatmap's hot blob.

Unit tests drive the tracker with synthetic heat grids (a Gaussian blob at a known spot); the payload tests
go through DashboardState with simulated packets.
"""
import math

import numpy as np

from csi.dashboard import DashboardState
from csi.heatmap import cell_centres
from csi.links import calibrate_links_report
from csi.room import load_room, stand_near_wall
from csi.simulate import simulate_room_links
from csi.tracker import LOST_S, Tracker, heat_measurement

W, D = 2.77, 1.84
CENTRES = cell_centres(28, 18, W, D)
T0 = 1_700_000_000.0


def blob(x, y, heat=0.8, sigma=0.3):
    d2 = ((CENTRES - [x, y]) ** 2).sum(axis=1)
    return heat * np.exp(-d2 / (2 * sigma ** 2))


def cold():
    return np.zeros(len(CENTRES))


def run(tracker, frames, t=T0, step=0.1):
    for values in frames:
        tracker.update(t, values)
        t += step
    return t


def dist(state, x, y):
    return math.hypot(state["x"] - x, state["y"] - y)


def test_a_cold_map_gives_no_measurement_and_no_track():
    tr = Tracker(W, D, CENTRES)
    run(tr, [cold()] * 20)
    assert heat_measurement(cold(), CENTRES) is None
    assert tr.state() is None


def test_the_measurement_is_the_blob_centre():
    x, y, heat = heat_measurement(blob(2.0, 0.6), CENTRES)
    assert math.hypot(x - 2.0, y - 0.6) < 0.1
    assert heat > 0.7


def test_the_track_settles_on_a_steady_blob():
    tr = Tracker(W, D, CENTRES)
    run(tr, [blob(2.0, 0.6)] * 30)
    assert dist(tr.state(), 2.0, 0.6) < 0.1


def test_the_track_follows_a_moving_blob():
    tr = Tracker(W, D, CENTRES)
    xs = np.linspace(0.4, 2.4, 41)  # 0.5 m/s at 10 steps/s
    t = run(tr, [blob(x, 0.9) for x in xs])
    s = tr.state(t)
    assert dist(s, 2.4, 0.9) < 0.35
    assert s["vx"] > 0.2 and abs(s["vy"]) < 0.2


def test_the_track_is_dropped_after_the_map_stays_cold():
    tr = Tracker(W, D, CENTRES)
    t = run(tr, [blob(1.0, 1.0)] * 10)
    t = run(tr, [cold()] * int(LOST_S * 10 * 0.5), t=t)
    held = tr.state(t)
    assert held is not None and held["confidence"] < 0.8   # coasting, fading
    run(tr, [cold()] * int(LOST_S * 10), t=t)
    assert tr.state() is None


def test_polling_faster_does_not_change_the_track():
    """Updates are rate-limited, so ten polls per heatmap step give the same dot as one."""
    frames = [blob(x, 0.9) for x in np.linspace(0.4, 2.4, 41)]
    once, often = Tracker(W, D, CENTRES), Tracker(W, D, CENTRES)
    run(once, frames)
    run(often, [f for f in frames for _ in range(10)], step=0.01)
    assert dist(often.state(), once.state()["x"], once.state()["y"]) < 0.05


def test_the_track_stays_inside_the_room():
    tr = Tracker(W, D, CENTRES)
    run(tr, [blob(x, 0.9) for x in np.linspace(1.5, W, 15)] + [cold()] * 10)
    s = tr.state()
    assert 0 <= s["x"] <= W and 0 <= s["y"] <= D


# ---------------------------------------------------------------------- payload, through the pipeline

ROOM = load_room()


def test_payload_track_is_none_in_an_empty_room_and_follows_motion_near_the_left_wall():
    thresholds = calibrate_links_report(list(simulate_room_links(person=None, duration_s=25, seed=10)),
                                        margin=1.5).thresholds
    state = DashboardState(ROOM, thresholds)
    empty = list(simulate_room_links(person=None, duration_s=12, seed=12))
    for receiver, pkt in empty:
        state.push(receiver, pkt)
    assert state.snapshot()["track"] is None

    person = list(simulate_room_links(person=stand_near_wall(ROOM, "left"), duration_s=6, seed=12, t0=T0 + 12))
    for receiver, pkt in person:
        state.push(receiver, pkt)
    track = state.snapshot()["track"]
    assert set(track) == {"x", "y", "vx", "vy", "confidence"}
    assert track["x"] < ROOM.width_m / 3
    assert 0 < track["confidence"] <= 1
