"""A bursty or busy clip must not yield a threshold from its quiet leftovers: calibration refuses the link when less
than MIN_CLEAN_FRACTION of its clip time is quiet, with a reason the operator can act on.

Scores come from the simulator through the text seam, as in tests/test_calibration_robust.py. The 'bursty' link is
a 25 s clip with 13 s of someone walking through: about half is quiet, so it is refused.
"""
import pytest

from csi.detect import MIN_CLEAN_FRACTION, CalibrationError, calibrate_clips, calibrate_packets
from csi.links import LinkId, calibrate_links_report
from csi.parser import parse_stream
from csi.simulate import SENDER_MACS, Segment, simulate_links, simulate_stream

REASON = "only {clean:.1f} s of {total:.1f} s was quiet; the link is bursty or someone was moving: record again"


def packets(scenario, seed=0):
    return list(parse_stream(simulate_stream(scenario=scenario, seed=seed)))


def bursty(seed=41):
    """5 s quiet, 13 s walking, 7 s quiet: 12 s of 25 s is quiet (48 %)."""
    return packets([Segment("empty", 5), Segment("motion", 13), Segment("empty", 7)], seed=seed)


def test_the_default_minimum_clean_fraction_is_one_half():
    assert MIN_CLEAN_FRACTION == 0.5


def test_a_bursty_clip_with_less_than_half_quiet_is_refused_with_the_reason_not_a_threshold():
    with pytest.raises(CalibrationError) as err:
        calibrate_packets(bursty())
    message = str(err.value)
    assert "was quiet" in message and "record again" in message
    assert "of 25.0 s" in message


def test_the_fraction_is_a_parameter_and_a_clip_passes_at_a_lower_one():
    cal = calibrate_clips([bursty()], min_clean_fraction=0.4)
    assert cal.clean_s == pytest.approx(12, abs=1.5)
    assert cal.threshold > 0


def test_a_clip_that_is_mostly_quiet_still_calibrates():
    cal = calibrate_packets(packets([Segment("empty", 20), Segment("motion", 4)], seed=42))
    assert cal.clean_s > 0.5 * (cal.clean_s + cal.rejected_s)


def test_the_link_report_lists_the_bursty_link_under_skipped_with_the_reason():
    tagged = list(simulate_links(senders=SENDER_MACS, receivers=("rx1",),
                                 scenario=[Segment("empty", 5), Segment("motion", 13), Segment("empty", 7)], seed=60))
    report = calibrate_links_report(clips=[tagged])
    label = LinkId("rx1", SENDER_MACS[0]).label

    assert label not in report.thresholds
    assert "was quiet" in report.skipped[label] and "record again" in report.skipped[label]


def test_the_live_calibration_refuses_the_same_bursty_link_with_the_same_reason():
    """rx1 has someone walking for the first 13 s of a 25 s clip; rx2 is empty throughout."""
    from csi.live_calibration import CalibrationJob, parse_request
    from csi.room import Walk, load_room
    from csi.simulate import simulate_room_links

    room = load_room()
    t0 = 1_700_000_000.0
    walk = Walk((room.wall_point("left"), room.wall_point("right")), speed_mps=0.5, loop=True)
    walking = list(simulate_room_links(person=walk, room=room, duration_s=13, seed=31, t0=t0))
    # one device timeline: the quiet part continues the counter (else it reads as a reboot); it runs past 25 s
    quiet = list(simulate_room_links(person=None, room=room, duration_s=13, seed=32, t0=t0 + 13,
                                     device_offset_us=13_000_000))
    stream = sorted(walking + quiet, key=lambda item: item[1].host_ts)
    # quiet-room mode (noisy is the dashboard default now): the busy link is refused
    job = CalibrationJob(parse_request({"clips": 1, "seconds": 25, "lead_s": 0, "noisy": False}), expected=[], now_mono=0.0,
                         current=lambda: {}, apply=lambda label, value: None)
    job.tick(t0, 0.0)
    for rx, p in stream:
        job.observe(rx, p, 0.0)

    rx1_labels = [label for label in job.skipped if label.startswith("rx1<-")]
    assert rx1_labels, job.skipped
    for label in rx1_labels:
        assert "was quiet" in job.skipped[label] and "record again" in job.skipped[label]
        assert label not in job.result
    assert any(label.startswith("rx2<-") for label in job.result)
