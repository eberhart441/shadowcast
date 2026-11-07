"""Robust calibration at the stream/packet seam: contaminated clips, pooled clips, nominal-rate loss and sparse links.

Everything is driven by the simulator (text seam -> parser -> packets). Expected values come from the scenario
(quiet vs walking) and from the known simulated loss, not from re-running the implementation.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from csi.detect import CalibrationError, MotionScorer, calibrate, calibrate_clips, calibrate_packets
from csi.links import LinkId, calibrate_links_report
from csi.parser import parse_stream
from csi.simulate import SENDER_MACS, Segment, simulate_links, simulate_stream

ROOT = Path(__file__).resolve().parents[1]
RECEIVERS = ("rx1", "rx2")


def packets(scenario, seed=0, **kwargs):
    return list(parse_stream(simulate_stream(scenario=scenario, seed=seed, **kwargs)))


def window_scores(pkts):
    scorer = MotionScorer()
    return np.array([s for s in (scorer.push(p) for p in pkts) if s is not None])


def contaminated(seed=41):
    """5 s quiet, 13 s walking through, 17 s quiet: a passer-by in a 35 s clip. Quiet is 63 %, above the
    50 % minimum; with less quiet the clip is refused instead (tests/test_calibration_clean_fraction.py)."""
    return packets([Segment("empty", 5), Segment("motion", 13), Segment("empty", 17)], seed=seed)


# --- 1. a passer-by inside the clip is rejected, not pooled into the threshold ---

def test_walk_through_is_rejected_and_threshold_comes_from_quiet_data():
    cal = calibrate_packets(contaminated())
    motion = window_scores(packets([Segment("motion", 10)], seed=42))
    assert cal.rejected_s >= 10
    assert cal.clean_s >= 8
    # the old rule (1.5 x p99 of every window) lands on the walking scores; the new one must sit well below them
    assert cal.threshold < np.percentile(motion, 5)


def test_clean_quiet_clip_rejects_nothing():
    cal = calibrate_packets(packets([Segment("empty", 20)], seed=43))
    assert cal.rejected_s == 0
    assert cal.clean_s == pytest.approx(19, abs=2)


def test_too_little_clean_data_raises_and_says_how_much_was_rejected():
    walk_heavy = packets([Segment("empty", 5), Segment("motion", 10)], seed=44)
    with pytest.raises(CalibrationError, match="clean") as err:
        calibrate_packets(walk_heavy)
    assert "rejected" in str(err.value)
    assert "record" in str(err.value).lower()


# --- 2. several clips: scored separately, pooled ---

def test_two_short_clips_pool_into_one_calibration_that_one_clip_cannot_give():
    a = packets([Segment("empty", 6)], seed=45)
    b = packets([Segment("empty", 6)], seed=46)
    with pytest.raises(CalibrationError):
        calibrate_packets(a)
    cal = calibrate_clips([a, b])
    assert cal.clips == 2
    assert cal.clean_s == pytest.approx(12, abs=1.5)
    assert cal.threshold > 0


def test_single_clip_through_clips_api_equals_single_clip_api():
    clip = packets([Segment("empty", 20)], seed=47)
    assert calibrate_clips([clip]).threshold == calibrate_packets(clip).threshold


def test_contaminated_clip_pooled_with_quiet_clip_still_gets_a_quiet_threshold():
    quiet = packets([Segment("empty", 20)], seed=48)
    alone = calibrate_packets(quiet).threshold
    pooled = calibrate_clips([contaminated(seed=49), quiet])
    motion = window_scores(packets([Segment("motion", 10)], seed=50))
    assert pooled.threshold < np.percentile(motion, 5)
    assert pooled.threshold < 3 * alone


# --- 3. loss uses the nominal send rate when given, and reads ~the true loss ---

def loss_stream(rate_hz, drop, seconds=25, seed=51):
    return simulate_stream(scenario=[Segment("empty", seconds)], rate_hz=rate_hz, seed=seed, drop_prob=drop)


def test_fifty_percent_loss_reads_about_fifty_percent_with_nominal_rate():
    assert calibrate(loss_stream(40, 0.5), send_rate_hz=40).loss == pytest.approx(0.5, abs=0.05)


def test_fifty_percent_loss_reads_about_fifty_percent_without_nominal_rate():
    assert calibrate(loss_stream(40, 0.5)).loss == pytest.approx(0.5, abs=0.07)


def test_seventeen_packets_per_second_link_is_calibrated_not_refused():
    cal = calibrate(loss_stream(40, 0.56, seconds=30), send_rate_hz=40)
    assert cal.pkt_rate_hz == pytest.approx(17.6, rel=0.15)
    assert cal.loss == pytest.approx(0.56, abs=0.05)


def test_sparse_link_below_fifteen_packets_per_second_is_refused_with_its_rate():
    with pytest.raises(CalibrationError, match="pkt/s"):
        calibrate(loss_stream(40, 0.85, seconds=30), send_rate_hz=40)


def test_extreme_loss_above_sixty_percent_is_refused():
    with pytest.raises(CalibrationError, match="loss"):
        calibrate(loss_stream(100, 0.7, seconds=25))


# --- 4. links: per-clip tagged lists, rejected seconds carried in the report ---

def tagged(scenario, seed):
    return list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS, scenario=scenario, seed=seed))


@pytest.mark.slow
def test_link_report_rejects_walk_through_and_reports_seconds():
    report = calibrate_links_report(tagged([Segment("empty", 5), Segment("motion", 13), Segment("empty", 17)], 60))
    rx = LinkId("rx1", SENDER_MACS[0]).label
    assert rx in report.thresholds
    assert report.rejected_s[rx] >= 10
    motion = window_scores(packets([Segment("motion", 10)], seed=61))
    assert report.thresholds[rx] < np.percentile(motion, 5)


def test_link_report_pools_clips_given_as_a_list_of_tagged_lists():
    clips = [tagged([Segment("empty", 6)], 62), tagged([Segment("empty", 6)], 63)]
    report = calibrate_links_report(clips=clips)
    assert set(report.thresholds) == {LinkId(r, m).label for r in RECEIVERS for m in SENDER_MACS}
    assert all(report.clean_s[label] == pytest.approx(12, abs=1.5) for label in report.thresholds)


# --- 5. CLI: --clips N records N clips with pauses and prints what was rejected ---

def run_links(*args, timeout=120):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "links_live.py"), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=timeout)


@pytest.mark.slow
def test_cli_clips_records_several_clips_and_reports_rejected_seconds(tmp_path):
    out = tmp_path / "links.json"
    r = run_links("--simulate-links", "--calibrate", "6", "--clips", "2", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "clip 1/2" in r.stdout and "clip 2/2" in r.stdout
    assert "next clip in 3 s" in r.stdout
    assert "rejected" in r.stdout and "clean" in r.stdout
    assert out.exists()
