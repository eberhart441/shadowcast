"""Per-link monitoring and calibration. Driven by simulated (receiver, packet) streams from the text seam."""
import pytest

from csi.detect import EMPTY, MOTION
from csi.links import (NO_SIGNAL, UNCALIBRATED, WARMUP, LinkId, LinkMonitor, calibrate_links,
                       calibrate_links_report, load_thresholds, save_thresholds)
from csi.simulate import SENDER_MACS, Segment, simulate_links

RECEIVERS = ("rx1", "rx2")
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}


def empty_links(seconds: float = 25, seed: int = 10):
    return list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS,
                               scenario=[Segment("empty", seconds)], seed=seed))


def motion_links(seconds: float = 12, seed: int = 12):
    return list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS,
                               scenario=[Segment("motion", seconds)], seed=seed))


def run_monitor(tagged, thresholds, **kw):
    """Push every tagged packet. Returns (monitor, {label: states seen}, host time of the last packet)."""
    monitor = LinkMonitor(thresholds, **kw)
    seen: dict[str, set[str]] = {}
    for receiver, pkt in tagged:
        state = monitor.push(receiver, pkt)
        seen.setdefault(LinkId(receiver, pkt.mac).label, set()).add(state)
    return monitor, seen, tagged[-1][1].host_ts


def test_link_label_is_receiver_and_last_octet_of_sender_mac():
    assert LinkId("rx1", "1a:00:00:00:00:01").label == "rx1<-01"
    assert LinkId("rx2", "1a:00:00:00:00:00").label == "rx2<-00"


def test_two_senders_on_two_receivers_are_four_separate_links():
    monitor, _, end = run_monitor(empty_links(seconds=10), {})
    rows = {row["id"]: row for row in monitor.snapshot(now=end)}
    assert set(rows) == LABELS
    for row in rows.values():
        assert row["pkt_rate"] == pytest.approx(100, rel=0.1)
        assert row["rssi"] is not None
        assert row["age_s"] == pytest.approx(0, abs=0.05)


def test_empty_room_keeps_every_link_empty_and_motion_drives_every_link_to_motion():
    cal = calibrate_links(empty_links(seed=10))
    assert set(cal) == LABELS and all(t > 0 for t in cal.values())

    _, seen_empty, _ = run_monitor(empty_links(seed=11), cal)
    for label, states in seen_empty.items():
        assert MOTION not in states, label
        assert EMPTY in states, label

    monitor, _, end = run_monitor(motion_links(), cal)
    for row in monitor.snapshot(now=end):
        assert row["state"] == MOTION, row["id"]
        assert row["threshold"] == pytest.approx(cal[row["id"]])


def test_link_silent_for_over_two_seconds_reports_no_signal_while_others_continue():
    tagged = empty_links(seconds=25, seed=21)
    cal = calibrate_links(empty_links(seed=20))
    silent = LinkId("rx1", SENDER_MACS[1]).label
    t0 = tagged[0][1].host_ts
    kept = [(r, p) for r, p in tagged
            if not (LinkId(r, p.mac).label == silent and p.host_ts - t0 > 5)]
    monitor, _, end = run_monitor(kept, cal)
    rows = {row["id"]: row for row in monitor.snapshot(now=end)}
    assert rows[silent]["state"] == NO_SIGNAL
    assert rows[silent]["age_s"] > 2.0
    assert rows[silent]["pkt_rate"] == 0.0
    for label in LABELS - {silent}:
        assert rows[label]["state"] in {EMPTY, MOTION, WARMUP}, label
        assert rows[label]["age_s"] < 0.5, label


def test_calibrate_links_skips_short_and_lossy_links_with_a_reason():
    tagged = empty_links(seconds=25, seed=30)
    short = LinkId("rx1", SENDER_MACS[1]).label
    lossy = LinkId("rx2", SENDER_MACS[0]).label
    t0 = tagged[0][1].host_ts
    kept, lossy_count = [], 0
    for receiver, pkt in tagged:
        label = LinkId(receiver, pkt.mac).label
        if label == short and pkt.host_ts - t0 > 5:
            continue
        if label == lossy:
            lossy_count += 1
            if lossy_count % 10 not in (0, 1, 2):  # keep 3 in a row, drop 70%: above the 60% limit
                continue
        kept.append((receiver, pkt))
    report = calibrate_links_report(kept)
    assert set(report.thresholds) == LABELS - {short, lossy}
    assert "short" in report.skipped[short]
    assert "loss" in report.skipped[lossy]


def test_calibrate_links_reports_expected_links_with_no_packets_as_skipped():
    report = calibrate_links_report(empty_links(seconds=12, seed=31),
                                    expected=[LinkId("rx3", SENDER_MACS[0])])
    assert "no packets" in report.skipped["rx3<-00"]


def test_thresholds_round_trip_through_json(tmp_path):
    path = tmp_path / "links.json"
    save_thresholds(path, {"rx1<-00": 3.5, "rx2<-01": 7.25})
    assert load_thresholds(path) == {"rx1<-00": 3.5, "rx2<-01": 7.25}


def test_link_without_threshold_is_reported_uncalibrated_not_guessed():
    monitor, seen, end = run_monitor(empty_links(seconds=5, seed=40), {"rx1<-00": 1.0})
    assert seen["rx2<-00"] == {UNCALIBRATED}
    rows = {row["id"]: row for row in monitor.snapshot(now=end)}
    assert rows["rx2<-01"]["threshold"] is None
    assert rows["rx2<-01"]["state"] == UNCALIBRATED
