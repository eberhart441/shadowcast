"""Calibration margin: an explicit parameter (default DEFAULT_MARGIN = 1.25), threshold = margin * p99.

Seams: the stream/packet seam (simulator -> text -> parser -> calibrate / detect_packets), the per-link
report, and the CLI boundary (subprocess). Expected values come from the scenario (quiet vs walking), the
proportionality of the rule, and the stated false-alarm tolerance, not from re-running the implementation.
"""
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from csi.detect import DEFAULT_MARGIN, MOTION, calibrate, calibrate_clips, calibrate_packets, detect_packets
from csi.links import calibrate_links_report, load_thresholds
from csi.parser import parse_stream
from csi.simulate import SENDER_MACS, Segment, simulate_links, simulate_stream

ROOT = Path(__file__).resolve().parents[1]
RECEIVERS = ("rx1", "rx2")
FALSE_ALARM_TOLERANCE = 0.02  # at margin 1.0, under 2% of held-out empty decisions may read MOTION


def packets(scenario, seed=0):
    return list(parse_stream(simulate_stream(scenario=scenario, seed=seed)))


# The clips below are simulated once per seed and shared by the tests that use them (read-only).
@lru_cache(maxsize=None)
def empty_clip(seed):
    return packets([Segment("empty", 20)], seed=seed)


@lru_cache(maxsize=None)
def held_out_empty_clip(seed):
    return packets([Segment("empty", 30)], seed=seed + 1000)


@lru_cache(maxsize=None)
def held_out_still_clip(seed):
    return packets([Segment("still", 30)], seed=seed + 2000)


def tagged(seconds, seed):
    return list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS,
                               scenario=[Segment("empty", seconds)], seed=seed))


def run(script, *args, timeout=150):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=timeout)


# --- 1. the parameter: default, record, proportional, validated ---

def test_default_margin_is_recorded_on_the_calibration():
    assert DEFAULT_MARGIN == 1.25
    cal = calibrate_packets(packets([Segment("empty", 20)], seed=1))
    assert cal.margin == DEFAULT_MARGIN


def test_threshold_is_margin_times_the_same_percentile_on_every_entry_point():
    quiet = packets([Segment("empty", 20)], seed=2)
    base = calibrate_packets(quiet, margin=1.0).threshold
    for m in (1.5, 3.0):
        cal = calibrate_packets(quiet, margin=m)
        assert cal.margin == m
        assert cal.threshold == pytest.approx(m * base, rel=1e-12)
        assert calibrate_clips([quiet], margin=m).threshold == pytest.approx(m * base, rel=1e-12)
    stream = simulate_stream(scenario=[Segment("empty", 20)], seed=2)
    assert calibrate(stream, margin=2.0).threshold == pytest.approx(2 * base, rel=1e-12)


@pytest.mark.slow
@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_non_positive_margin_is_a_value_error_naming_the_margin(bad):
    clip = packets([Segment("empty", 20)], seed=3)
    with pytest.raises(ValueError, match="margin"):
        calibrate_packets(clip, margin=bad)
    with pytest.raises(ValueError, match="margin"):
        calibrate_clips([clip], margin=bad)
    with pytest.raises(ValueError, match="margin"):
        calibrate_links_report(tagged(20, 3), margin=bad)


def test_link_report_records_margin_and_scales_each_links_threshold():
    clip = tagged(20, 4)
    base = calibrate_links_report(clip)
    doubled = calibrate_links_report(clip, margin=2 * DEFAULT_MARGIN)
    assert base.margin == DEFAULT_MARGIN
    assert doubled.margin == 2 * DEFAULT_MARGIN
    assert set(base.thresholds) == set(doubled.thresholds) and base.thresholds
    for label, t in base.thresholds.items():
        assert doubled.thresholds[label] == pytest.approx(2 * t, rel=1e-12)
        assert doubled.calibrations[label].margin == 2 * DEFAULT_MARGIN


# --- 2. behaviour at margin 1.0 on the simulator ---

def test_margin_one_still_detects_simulated_motion_as_motion():
    cal = calibrate_packets(packets([Segment("empty", 20)], seed=10), margin=1.0)
    states = [r.state for r in detect_packets(packets([Segment("motion", 10)], seed=11), cal.threshold)]
    assert states[-1] == MOTION
    assert sum(s == MOTION for s in states) > 0.8 * len(states)


HELD_OUT_SEEDS = range(200, 205)


def held_out_false_alarm_rate(margin: float) -> float:
    """Mean fraction of held-out empty decisions reading MOTION, each clip calibrated on its own empty clip."""
    rates = []
    for seed in HELD_OUT_SEEDS:
        cal = calibrate_packets(empty_clip(seed), margin=margin)
        held_out = [r.state for r in detect_packets(held_out_empty_clip(seed), cal.threshold)]
        rates.append(sum(s == MOTION for s in held_out) / len(held_out))
    return sum(rates) / len(rates)


@pytest.mark.slow
def test_margin_1_5_keeps_held_out_empty_within_tolerance():
    assert held_out_false_alarm_rate(1.5) < FALSE_ALARM_TOLERANCE


def test_default_margin_keeps_held_out_empty_within_tolerance():
    # margin 1.0 fails this on the simulator (mean ~39% MOTION over 20 seeds); 1.25 gives 0%
    assert DEFAULT_MARGIN == 1.25
    assert held_out_false_alarm_rate(DEFAULT_MARGIN) < FALSE_ALARM_TOLERANCE


def still_person_motion_rate(margin: float) -> float:
    """Mean fraction of decisions reading MOTION for a still person, calibrated on an empty clip."""
    rates = []
    for seed in HELD_OUT_SEEDS:
        cal = calibrate_packets(empty_clip(seed), margin=margin)
        st = [r.state for r in detect_packets(held_out_still_clip(seed), cal.threshold)]
        rates.append(sum(s == MOTION for s in st) / len(st))
    return sum(rates) / len(rates)


@pytest.mark.slow
def test_margin_1_5_does_not_read_a_still_person_as_motion():
    assert still_person_motion_rate(1.5) < FALSE_ALARM_TOLERANCE


def test_default_margin_does_not_read_a_still_person_as_motion():
    # margin 1.0 reads a simulated breathing person as MOTION ~68% of the time; 1.25 gives 0%
    assert still_person_motion_rate(DEFAULT_MARGIN) < FALSE_ALARM_TOLERANCE


@pytest.mark.slow
def test_larger_margin_never_detects_more_motion_on_the_same_session():
    calib = packets([Segment("empty", 20)], seed=14)
    session = packets([Segment("empty", 15), Segment("motion", 5), Segment("empty", 10),
                       Segment("motion", 5)], seed=15)
    motion_counts = []
    for m in (0.5, 1.0, 1.5, 2.0, 4.0):
        threshold = calibrate_packets(calib, margin=m).threshold
        motion_counts.append(sum(r.state == MOTION for r in detect_packets(session, threshold)))
    assert motion_counts == sorted(motion_counts, reverse=True)
    assert motion_counts[0] > motion_counts[-1]  # the margin really changes the outcome here


# --- 3. CLI: --margin on links_live.py and detect_live.py; file format unchanged ---

@pytest.mark.slow
def test_links_live_margin_scales_saved_thresholds_and_is_printed(tmp_path):
    default_out, doubled_out = tmp_path / "a.json", tmp_path / "b.json"
    a = run("links_live.py", "--simulate-links", "--calibrate", "15", "--margin", "1", "--out", str(default_out))
    b = run("links_live.py", "--simulate-links", "--calibrate", "15", "--margin", "2",
            "--out", str(doubled_out))
    assert a.returncode == 0, a.stderr
    assert b.returncode == 0, b.stderr
    assert "margin 1.00" in a.stdout and "margin 2.00" in b.stdout
    t_default, t_doubled = load_thresholds(default_out), load_thresholds(doubled_out)
    assert set(t_default) == set(t_doubled) and t_default
    for label in t_default:
        assert t_doubled[label] == pytest.approx(2 * t_default[label], rel=1e-9)
    # the thresholds file stays a plain {label: number} object
    assert all(isinstance(v, float) for v in json.loads(doubled_out.read_text()).values())


@pytest.mark.slow
def test_links_live_rejects_non_positive_margin_before_writing(tmp_path):
    out = tmp_path / "links.json"
    r = run("links_live.py", "--simulate-links", "--calibrate", "15", "--margin", "0", "--out", str(out))
    assert r.returncode != 0
    assert "margin" in r.stderr
    assert not out.exists()


@pytest.mark.slow
def test_detect_live_margin_scales_threshold_and_is_printed(tmp_path):
    from csi.detect import load_threshold
    default_out, doubled_out = tmp_path / "a.json", tmp_path / "b.json"
    a = run("detect_live.py", "--simulate", "--calibrate", "15", "--margin", "1", "--out", str(default_out))
    b = run("detect_live.py", "--simulate", "--calibrate", "15", "--margin", "2", "--out", str(doubled_out))
    assert a.returncode == 0, a.stderr
    assert b.returncode == 0, b.stderr
    assert "margin 1.00" in a.stdout and "margin 2.00" in b.stdout
    assert load_threshold(doubled_out) == pytest.approx(2 * load_threshold(default_out), rel=1e-9)


@pytest.mark.slow
def test_detect_live_rejects_non_positive_margin(tmp_path):
    r = run("detect_live.py", "--simulate", "--calibrate", "15", "--margin", "-1",
            "--out", str(tmp_path / "c.json"))
    assert r.returncode != 0
    assert "margin" in r.stderr
