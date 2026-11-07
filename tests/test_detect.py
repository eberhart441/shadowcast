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
    # 70% loss is above the 60% limit; 50% is now accepted (see test_calibration_robust.py)
    with pytest.raises(CalibrationError, match="loss"):
        calibrate(empty_stream(drop_prob=0.7))


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


# --- device_ts counter events: the ESP32 local_timestamp is 32-bit and restarts on reboot ---

WRAP = 2 ** 32


def scenario_stream(clock, seconds=30, seed=9, scenario=None, realtime=False):
    segs = scenario or [Segment("empty", seconds)]
    return simulate_stream(scenario=segs, seed=seed, device_clock=clock)


def test_device_ts_reset_to_zero_keeps_producing_readings():
    # receiver reboots at 15 s: local_timestamp goes back to ~0 and counts up again
    reboot_us = 15_000_000
    stream = list(scenario_stream(lambda t: t if t < reboot_us else t - reboot_us, seconds=30))
    threshold = calibrated_threshold()
    readings = list(detect(stream, threshold))
    reboot_host = stream[1500][0]  # 15 s at 100 Hz (header line is first)
    after = [r for r in readings if r.host_ts > reboot_host + 1.5]
    assert len(after) > 1000
    assert {r.state for r in after} == {EMPTY}


def test_device_ts_reset_during_motion_recovers_to_motion():
    reboot_us = 10_000_000
    segs = [Segment("empty", 10), Segment("motion", 10)]
    stream = list(scenario_stream(lambda t: t if t < reboot_us else t - reboot_us, scenario=segs))
    readings = list(detect(stream, calibrated_threshold()))
    assert readings[-1].state == MOTION


def test_device_ts_wrap_matches_unwrapped_scores_exactly():
    # counter wraps past 2**32 three seconds in; the scores must not change at all
    start = WRAP - 3_000_000
    wrapped = list(parse_stream(scenario_stream(lambda t: (t + start) % WRAP, seconds=10)))
    plain = list(parse_stream(scenario_stream(lambda t: t, seconds=10)))
    assert any(p.device_ts < start for p in wrapped)  # the wrap really happened
    scorer_w, scorer_p = MotionScorer(), MotionScorer()
    a = [s for s in (scorer_w.push(p) for p in wrapped) if s is not None]
    b = [s for s in (scorer_p.push(p) for p in plain) if s is not None]
    assert len(a) == len(b) > 500
    np.testing.assert_allclose(a, b, rtol=1e-9)


def test_device_ts_wrap_keeps_breathing_readings_going():
    start = WRAP - 5_000_000
    stream = list(scenario_stream(lambda t: (t + start) % WRAP, seconds=25,
                                  scenario=[Segment("still", 25)]))
    readings = list(detect(stream, calibrated_threshold(), breathing=True))
    assert readings[-1].enough_data
    assert MOTION not in {r.state for r in readings}


def test_device_ts_reset_restarts_breathing_window():
    reboot_us = 20_000_000
    stream = list(scenario_stream(lambda t: t if t < reboot_us else t - reboot_us,
                                  scenario=[Segment("still", 40)]))
    readings = list(detect(stream, calibrated_threshold(), breathing=True))
    assert readings[-1].enough_data  # a full window exists again after the reboot
