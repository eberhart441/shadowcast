"""The transceiver heatmap: every link's evidence imaged over the room by regularised radio tomography.

Driven through the transceiver seam (simulated round broadcasts into DashboardState) for accuracy and through
Heatmap.advance (the step the dashboard runs every 0.1 s of host time) for the per-frame cost. Rooms: the frozen
8-node and 30-node rooms (ADR-0017), both 4 x 3 m, so more nodes means more links over the same floor.
"""
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.heatmap import Heatmap, cell_centres
from csi.links import room_link_ids
from csi.room import Stand, Walk, load_room
from csi.simulate import simulate_rounds

FIXTURES = Path(__file__).parent / "fixtures"
ROOMS = {8: load_room(FIXTURES / "room_tr8.json"), 30: load_room(FIXTURES / "room_tr30.json")}
ROOM30 = ROOMS[30]
T0 = 1_700_000_000.0
# An empty room first: each link's score needs 1 s, its adaptive detector 10 s (WARMUP until then, csi/adaptive.py)
# and its quiet baseline 10 s of EMPTY history before it puts heat on the map (ADR-0011).
QUIET_S = 22.0
WALK = Walk(((0.8, 0.7), (3.2, 0.7), (3.2, 2.3), (0.8, 2.3)), speed_mps=0.5, loop=True)   # round the room
WALK_S = 8.0
# Stated errors: the largest distance from the walker to the heatmap's hottest cell, sampled every 0.5 s of the
# walk (the simulator's walker, not a person on a rig: the 8-node labelled walk, #20, measures that). Measured with
# seeds 0-2: 0.45 m at 8 nodes and 0.26-0.29 m at 30 (medians 0.14-0.16 m at both).
PEAK_ERROR_8_M = 0.6
PEAK_ERROR_30_M = 0.4


def centres_of(payload):
    heat = payload["heatmap"]
    return cell_centres(heat["cols"], heat["rows"], heat["extents"]["x_max"], heat["extents"]["y_max"])


def hottest(payload):
    """(x, y) of the hottest cell of the payload's heatmap."""
    return tuple(centres_of(payload)[int(np.argmax(payload["heatmap"]["cells"]))])


@lru_cache(maxsize=None)
def walk(nodes, seed=0):
    """(true position, payload) every 0.5 s of the walk from 1 s in, through the transceiver seam.

    The 30-node rig rounds at 5 Hz here, to keep the run short: the heatmap reads each link's latest score, so the
    round rate changes how often evidence updates, not where it points."""
    room = ROOMS[nodes]
    round_hz = 20.0 if nodes <= 8 else 5.0
    stream = simulate_rounds(room, WALK, duration_s=QUIET_S + WALK_S, quiet_first_s=QUIET_S, round_hz=round_hz,
                             seed=seed, t0=T0)
    state = DashboardState(room, mode="auto")
    times = list(np.arange(1.0, WALK_S, 0.5))
    out, pending = [], list(times)
    for host_ts, data in stream:
        while pending and host_ts > T0 + QUIET_S + pending[0]:
            t = pending.pop(0)
            out.append((tuple(WALK.position(np.array([t]))[0]), state.snapshot(T0 + QUIET_S + t)))
        state.push_broadcast(host_ts, data)
    return out


def peak_errors(run):
    return [float(np.hypot(*np.subtract(hottest(payload), truth))) for truth, payload in run]


def test_the_heatmap_peak_follows_a_walker_in_the_8_node_room():
    run = walk(8)
    assert all(payload["heatmap"]["max"] > 0.2 for _, payload in run)
    assert max(peak_errors(run)) <= PEAK_ERROR_8_M


@pytest.mark.slow
def test_the_heatmap_peak_follows_a_walker_in_the_30_node_room_more_closely_than_with_8():
    run30 = walk(30)
    assert all(payload["heatmap"]["max"] > 0.2 for _, payload in run30)
    assert max(peak_errors(run30)) <= PEAK_ERROR_30_M
    assert max(peak_errors(run30)) < max(peak_errors(walk(8)))
    assert np.mean(peak_errors(run30)) < np.mean(peak_errors(walk(8)))


# Per-frame budget at 30 nodes (870 links): one heatmap step (every link's baseline and evidence, then the image),
# with a full 60 s quiet baseline behind every link, must take at most a tenth of the dashboard's 0.2 s frame
# interval. The heatmap steps every 0.1 s of host time and once more per frame, so it then uses at most about a
# third of the frame interval in the worst case; about 6 ms per step was measured on the development PC.
HEATMAP_STEP_BUDGET_S = 0.020


@pytest.mark.slow
def test_one_heatmap_step_at_30_nodes_fits_the_per_frame_budget():
    """Measured as CPU time of this process over the last 100 steps, so other processes on a busy machine (parallel
    test workers) do not count against it."""
    labels = [link_id.label for link_id, _, _ in room_link_ids(ROOM30)]
    assert len(labels) == 870
    heat = Heatmap(ROOM30)
    rng = np.random.default_rng(0)
    steps, timed = 700, 100                        # 70 s at the 0.1 s step: the 60 s baselines are full
    frames = [[{"id": label, "state": "EMPTY", "score": float(s), "age_s": 0.0}
               for label, s in zip(labels, rng.lognormal(0.0, 0.2, len(labels)))] for _ in range(timed)]
    for step in range(steps - timed):
        heat.advance(step * 0.1, frames[step % timed])
    started = time.process_time()
    for step in range(steps - timed, steps):
        heat.advance(step * 0.1, frames[step % timed])
    per_step = (time.process_time() - started) / timed
    assert per_step <= HEATMAP_STEP_BUDGET_S


def test_the_kalman_track_follows_the_heatmap_peak_in_the_8_node_room():
    run = walk(8)
    assert all(payload["track"] is not None for _, payload in run)
    to_peak = [np.hypot(p["track"]["x"] - hottest(p)[0], p["track"]["y"] - hottest(p)[1]) for _, p in run]
    to_walker = [np.hypot(p["track"]["x"] - truth[0], p["track"]["y"] - truth[1]) for truth, p in run]
    assert np.median(to_peak) <= 0.2          # measured about 0.09 m: the dot sits on the hot spot
    assert max(to_walker) <= PEAK_ERROR_8_M    # and, smoothing it, stays as close to the walker as the peak does


def two_people(nodes, a, b, seconds=3.0, seed=0):
    """The payload ``seconds`` after two people appear standing at ``a`` and ``b``."""
    room = ROOMS[nodes]
    people = (Stand(*a), Stand(*b))
    round_hz = 20.0 if nodes <= 8 else 5.0
    stream = simulate_rounds(room, people, duration_s=QUIET_S + seconds, quiet_first_s=QUIET_S, round_hz=round_hz,
                             seed=seed, t0=T0)
    state = DashboardState(room, mode="auto")
    for host_ts, data in stream:
        state.push_broadcast(host_ts, data)
    return state.snapshot(T0 + QUIET_S + seconds)


def heat_near(payload, point, radius_m=0.3):
    """The hottest cell within ``radius_m`` of ``point``."""
    centres, cells = centres_of(payload), np.array(payload["heatmap"]["cells"])
    near = np.hypot(*(centres - np.array(point)).T) <= radius_m
    return float(cells[near].max())


@pytest.mark.slow
def test_two_people_far_apart_show_as_two_hot_spots_in_the_30_node_room():
    a, b = (1.0, 0.75), (3.0, 2.25)
    payload = two_people(30, a, b)
    peak_a, peak_b = heat_near(payload, a), heat_near(payload, b)
    assert min(peak_a, peak_b) >= 0.5 * payload["heatmap"]["max"]          # both are hot spots, neither a shadow
    for cold in ((2.0, 1.5), (1.0, 2.25), (3.0, 0.75)):                   # between them, and the other corners
        assert heat_near(payload, cold) < 0.5 * min(peak_a, peak_b), cold
