import pytest

from csi.detect import EMPTY, MOTION, STILL_PRESENT, detect
from csi.simulate import Segment, simulate_stream

THRESHOLD = 2.0  # same manual threshold as test_detect.py
BPM_TRUE = 18.0  # 0.3 Hz breathing in the simulator
BPM_TOL = 2.0    # breaths per minute
WINDOW_S = 15.0


def readings(scenario, seed: int = 1, **kwargs):
    stream = simulate_stream(scenario=scenario, seed=seed, **kwargs)
    out = list(detect(stream, THRESHOLD))
    assert out
    return out


def last_reading(state: str, seconds: float = 30, seed: int = 1, **kwargs):
    return readings([Segment(state, seconds)], seed=seed, **kwargs)[-1]


def test_still_yields_still_present_at_breathing_rate():
    r = last_reading("still")
    assert r.state == STILL_PRESENT
    assert r.bpm == pytest.approx(BPM_TRUE, abs=BPM_TOL)


def test_empty_never_yields_still_present():
    states = {r.state for r in readings([Segment("empty", 30)], seed=2)}
    assert STILL_PRESENT not in states


def test_motion_yields_motion_not_breathing():
    states = {r.state for r in readings([Segment("motion", 30)], seed=2)}
    assert STILL_PRESENT not in states and MOTION in states


def test_no_breathing_claimed_before_fifteen_seconds():
    early = readings([Segment("still", 10)])
    assert all(not r.enough_data and r.bpm is None for r in early)


def test_breathing_available_once_fifteen_seconds_exist():
    late = readings([Segment("still", 25)])[-1]
    assert late.enough_data and late.state == STILL_PRESENT


def test_breathing_rate_survives_modest_drops():
    r = last_reading("still", seconds=30, seed=3, drop_prob=0.1)
    assert r.state == STILL_PRESENT
    assert r.bpm == pytest.approx(BPM_TRUE, abs=BPM_TOL)


def test_breathing_reported_after_motion_is_not_contaminated_by_it():
    rs = readings([Segment("still", 25), Segment("motion", 8), Segment("still", 25)], seed=6)
    still_bpm = [r.bpm for r in rs if r.state == STILL_PRESENT]
    assert still_bpm
    assert all(b == pytest.approx(BPM_TRUE, abs=BPM_TOL) for b in still_bpm)


def test_motion_after_still_overrides_breathing():
    r = readings([Segment("still", 30), Segment("motion", 15)], seed=4)[-1]
    assert r.state == MOTION


def test_still_state_stable_without_flicker():
    rs = readings([Segment("still", 40)], seed=5)
    start = next(r.host_ts for r in rs if r.enough_data)
    settled = [r.state for r in rs if r.host_ts - start >= 2.0]  # allow the debounce hold
    changes = sum(a != b for a, b in zip(settled, settled[1:]))
    assert settled and settled[0] == STILL_PRESENT and changes == 0
