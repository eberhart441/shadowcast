import numpy as np
import pytest

from csi.detect import (EMPTY, LEGACY_SCORER, LIVE_SCORER, MOTION, STILL_PRESENT, CalibrationError, MotionScorer,
                        ScorerConfig, calibrate, detect, detect_packets, load_threshold)
from csi.parser import CSIParser, parse_stream
from csi.simulate import Segment, simulate_stream

THRESHOLD = 2.0  # manual threshold: between simulated empty/still (~0.2) and motion (~7+)
# These tests check that an empty room, a still person and walking separate once calibrated on a different
# empty clip. They pin a 1.5 margin on purpose: at the default 1.0 the simulator's held-out empty clips and
# still person cross a single-clip p99 (measured in tests/test_calibration_margin.py), which is not what
# they test.
TEST_MARGIN = 1.5


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
    cal = calibrate(empty_stream(), margin=TEST_MARGIN)
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
    return calibrate(empty_stream(), margin=TEST_MARGIN).threshold


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


# --- the scorer configuration: one live configuration, recorded by every calibration ---

def median_score(packets, config):
    scorer = MotionScorer.from_config(config)
    return float(np.median([s for s in (scorer.push(p) for p in packets) if s is not None]))


def test_the_default_scorer_is_the_live_configuration():
    assert MotionScorer().config == LIVE_SCORER
    assert (LIVE_SCORER.window_s, LIVE_SCORER.tau_s, LIVE_SCORER.stages) == (2.0, 0.32, 3)
    assert LIVE_SCORER.trapezoid is True
    assert ScorerConfig.from_dict(LIVE_SCORER.as_dict()) == LIVE_SCORER
    assert LIVE_SCORER != LEGACY_SCORER


def test_the_trapezoidal_update_keeps_the_empty_score_scale_across_packet_rates():
    # the same empty room sent at 100 and at 25 packets/s: the update-then-subtract stages lose a rate-dependent
    # share of the white noise (about half at 25 Hz), the trapezoidal ones almost none
    def ratio(config):
        fast = list(parse_stream(simulate_stream(scenario=[Segment("empty", 20)], rate_hz=100.0, seed=3)))
        slow = list(parse_stream(simulate_stream(scenario=[Segment("empty", 20)], rate_hz=25.0, seed=3)))
        return median_score(slow, config) / median_score(fast, config)

    assert 0.8 < ratio(LIVE_SCORER) < 1.25
    assert ratio(LEGACY_SCORER) < 0.7


def test_the_median_prefilter_removes_single_sample_spikes():
    import dataclasses
    rng = np.random.default_rng(1)
    clean = list(parse_stream(simulate_stream(scenario=[Segment("empty", 20)], seed=7)))
    spiky = []
    for p in clean:
        amp = np.array(p.amplitude, dtype=np.float64)
        if rng.random() < 0.05:  # one subcarrier of one packet in 20 jumps to 3x
            amp[rng.integers(amp.size)] *= 3.0
        spiky.append(dataclasses.replace(p, amplitude=amp))
    with_median = dataclasses.replace(LIVE_SCORER, median3=True)
    without = dataclasses.replace(LIVE_SCORER, median3=False)
    assert median_score(spiky, with_median) < 1.5 * median_score(clean, with_median)
    assert median_score(spiky, without) > 3 * median_score(clean, without)


def test_a_calibration_records_the_scorer_it_was_made_with():
    live = calibrate(empty_stream(), margin=TEST_MARGIN)
    legacy = calibrate(empty_stream(), margin=TEST_MARGIN, scorer=LEGACY_SCORER)
    assert live.scorer == LIVE_SCORER and legacy.scorer == LEGACY_SCORER
    assert live.threshold != legacy.threshold                      # the scales differ: never mix them


def test_detection_scores_with_the_scorer_it_is_given_and_the_live_one_by_default():
    packets = list(parse_stream(empty_stream(seconds=8)))
    for config in (LIVE_SCORER, LEGACY_SCORER):
        scorer = MotionScorer.from_config(config)
        expected = [s for s in (scorer.push(p) for p in packets) if s is not None]
        assert [r.score for r in detect_packets(packets, 1e9, scorer=config)] == expected
    assert [r.score for r in detect_packets(packets, 1e9)] == [r.score for r in
                                                                detect_packets(packets, 1e9, scorer=LIVE_SCORER)]


def test_the_calibration_file_records_the_scorer_and_old_files_have_none(tmp_path):
    from csi.detect import load_calibration_scorer
    path = tmp_path / "cal.json"
    calibrate(empty_stream(), margin=TEST_MARGIN).save(path)
    assert load_calibration_scorer(path) == LIVE_SCORER
    path.write_text('{"threshold": 13.1, "rate_hz": 90.9, "duration_s": 30.0, "loss": 0.01}')  # before the record
    assert load_calibration_scorer(path) is None
    assert load_threshold(path) == 13.1


def test_a_scorer_note_names_a_mismatch_or_a_missing_record_and_nothing_for_the_live_scorer():
    from csi.detect import scorer_note
    assert scorer_note(LIVE_SCORER, "links.json") is None
    other = scorer_note(LEGACY_SCORER, "links.json")
    assert "another motion scorer" in other and "window 1 s" in other and "recalibrate" in other
    assert "does not record" in scorer_note(None, "links.json")
