import numpy as np
import pytest

from csi.detect import EMPTY, MOTION, STILL_PRESENT, CalibrationError, MotionScorer, calibrate, detect, detect_packets, load_threshold
from csi.parser import CSIParser, parse_stream
from csi.simulate import Segment, simulate_stream

THRESHOLD = 2.0  # manual threshold: between simulated empty/still (~0.2) and motion (~7+)


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


# --- calibrated threshold on the stream seam: breathing must not read as motion ---

def calibrated_threshold() -> float:
    return calibrate(empty_stream()).threshold


def still_stream(seconds: float = 30, seed: int = 6):
    return simulate_stream(scenario=[Segment("still", seconds)], seed=seed)


def test_calibrated_empty_is_empty_throughout():
    stream = simulate_stream(scenario=[Segment("empty", 30)], seed=8)
    states = {r.state for r in detect(stream, calibrated_threshold())}
    assert states == {EMPTY}


def test_still_person_is_not_motion_after_calibrating_on_empty():
    states = [r.state for r in detect(still_stream(), calibrated_threshold())]
    assert MOTION not in states


def test_still_person_is_still_present_only_when_breathing_is_opted_in():
    states = [r.state for r in detect(still_stream(), calibrated_threshold(), breathing=True)]
    assert MOTION not in states
    assert states[-1] == STILL_PRESENT


# --- motion-only is the default: breathing is opt-in and STILL_PRESENT never appears otherwise ---

def test_default_mode_never_yields_still_present_for_a_breathing_still_person():
    states = {r.state for r in detect(still_stream(seconds=40), calibrated_threshold())}
    assert STILL_PRESENT not in states
    assert states == {EMPTY}


def test_default_mode_never_yields_still_present_on_the_packet_api():
    packets = list(parse_stream(still_stream(seconds=40)))
    states = {r.state for r in detect_packets(packets, calibrated_threshold())}
    assert STILL_PRESENT not in states


def test_default_mode_reports_no_breathing_fields():
    readings = list(detect(still_stream(seconds=30), calibrated_threshold()))
    assert readings and all(r.bpm is None and not r.enough_data for r in readings)


def test_breathing_is_not_computed_unless_opted_in(monkeypatch):
    import csi.detect as detect_module

    def fail(*args, **kwargs):
        raise AssertionError("breathing was computed in motion-only mode")

    monkeypatch.setattr(detect_module, "estimate_breathing", fail)
    assert list(detect(still_stream(seconds=30), calibrated_threshold()))


def test_motion_after_calibrating_on_empty_is_motion():
    stream = simulate_stream(scenario=[Segment("motion", 10)], seed=6)
    assert list(detect(stream, calibrated_threshold()))[-1].state == MOTION


def test_breathing_does_not_raise_motion_score_above_empty_level():
    def p99_scores(stream):
        scorer = MotionScorer()
        scores = [s for s in (scorer.push(p) for p in parse_stream(stream)) if s is not None]
        return np.percentile(scores, 99)

    empty = p99_scores(simulate_stream(scenario=[Segment("empty", 30)], seed=8))
    still = p99_scores(still_stream())
    assert still < 1.5 * empty
