import pytest

from csi.detect import EMPTY, MOTION, CalibrationError, calibrate, detect, load_threshold
from csi.parser import CSIParser
from csi.simulate import Segment, simulate_stream

THRESHOLD = 2.0  # manual threshold: between simulated empty (~0.2) and motion (~9)


def final_state(state: str, seconds: float = 10, threshold: float = THRESHOLD, seed: int = 1) -> str:
    stream = simulate_stream(scenario=[Segment(state, seconds)], seed=seed)
    readings = list(detect(stream, threshold))
    assert readings
    return readings[-1].state


def test_empty_yields_empty():
    assert final_state("empty") == EMPTY


def test_motion_yields_motion():
    assert final_state("motion") == MOTION


def empty_stream(seconds: float = 20, **kwargs):
    return simulate_stream(scenario=[Segment("empty", seconds)], seed=5, **kwargs)


def test_calibrated_threshold_separates_empty_and_motion(tmp_path):
    cal = calibrate(empty_stream())
    cal.save(tmp_path / "t.json")
    threshold = load_threshold(tmp_path / "t.json")
    assert threshold == cal.threshold
    assert final_state("empty", threshold=threshold, seed=6) == EMPTY
    assert final_state("motion", threshold=threshold, seed=6) == MOTION


def test_calibration_estimates_rate_from_timestamps():
    assert calibrate(empty_stream()).rate_hz == pytest.approx(100, rel=0.02)


def test_calibration_rejects_too_short_input():
    with pytest.raises(CalibrationError, match="short"):
        calibrate(empty_stream(seconds=3))


def test_calibration_rejects_too_lossy_input():
    with pytest.raises(CalibrationError, match="loss"):
        calibrate(empty_stream(drop_prob=0.5))


def test_state_changes_once_at_motion_onset_without_flicker():
    stream = simulate_stream(scenario=[Segment("empty", 10), Segment("motion", 10)], seed=7)
    states = [r.state for r in detect(stream, THRESHOLD)]
    changes = sum(a != b for a, b in zip(states, states[1:]))
    assert states[0] == EMPTY and states[-1] == MOTION and changes == 1


def test_malformed_lines_counted_and_do_not_stop_detection():
    stream = list(empty_stream(seconds=10))
    stream[50:50] = [(stream[50][0], "CSI_DATA,garbage")] * 3
    parser = CSIParser()
    assert list(detect(stream, THRESHOLD, parser))
    assert parser.stats.malformed == 3
