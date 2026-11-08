"""Per-link monitoring and calibration. Driven by simulated (receiver, packet) streams from the text seam."""
import pytest

from csi.detect import EMPTY, LIVE_SCORER, MOTION, MotionScorer
from csi.links import (NO_SIGNAL, UNCALIBRATED, WARMUP, LinkId, LinkMonitor, calibrate_links,
                       calibrate_links_report, load_thresholds, rate_notes, read_thresholds, save_thresholds,
                       threshold_file_notes)
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


@pytest.mark.slow
def test_empty_room_keeps_every_link_empty_and_motion_drives_every_link_to_motion():
    # margin 1.5 (not the default 1.0): at 1.0 a held-out empty clip crosses a single-clip p99 on the simulator
    cal = calibrate_links_report(empty_links(seed=10), margin=1.5).thresholds
    assert set(cal) == LABELS and all(t > 0 for t in cal.values())

    _, seen_empty, _ = run_monitor(empty_links(seed=11), cal)
    for label, states in seen_empty.items():
        assert MOTION not in states, label
        assert EMPTY in states, label

    monitor, _, end = run_monitor(motion_links(), cal)
    for row in monitor.snapshot(now=end):
        assert row["state"] == MOTION, row["id"]
        assert row["threshold"] == pytest.approx(cal[row["id"]])


@pytest.mark.slow
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


# ---------------------------------------------------------------- the calibration record in links.json


def test_a_calibration_saved_with_its_record_keeps_scorer_rates_and_time_and_still_loads(tmp_path):
    path = tmp_path / "links.json"
    report = calibrate_links_report(empty_links(seconds=12, seed=50))
    save_thresholds(path, report.thresholds, report.record())
    assert load_thresholds(path) == pytest.approx(report.thresholds)
    recorded = read_thresholds(path)
    assert recorded.thresholds == pytest.approx(report.thresholds)
    assert recorded.scorer == LIVE_SCORER
    assert set(recorded.pkt_rate_hz) == LABELS
    assert all(rate == pytest.approx(100, rel=0.1) for rate in recorded.pkt_rate_hz.values())
    assert recorded.margin == report.margin and recorded.noisy is False
    assert recorded.saved_at and recorded.saved_at.startswith("20")
    assert threshold_file_notes(recorded) == []


def test_an_old_plain_thresholds_file_still_loads_and_is_flagged_as_unrecorded(tmp_path):
    path = tmp_path / "links.json"
    path.write_text('{"rx1<-00": 3.5, "rx2<-01": 7.25}')
    assert load_thresholds(path) == {"rx1<-00": 3.5, "rx2<-01": 7.25}
    recorded = read_thresholds(path)
    assert recorded.scorer is None and recorded.pkt_rate_hz == {}
    (note,) = threshold_file_notes(recorded, "links.json")
    assert "does not record" in note


def test_a_file_from_another_scorer_is_flagged(tmp_path):
    from csi.detect import LEGACY_SCORER
    path = tmp_path / "links.json"
    report = calibrate_links_report(empty_links(seconds=12, seed=51), scorer=LEGACY_SCORER)
    save_thresholds(path, report.thresholds, report.record())
    (note,) = threshold_file_notes(read_thresholds(path), "links.json")
    assert "another motion scorer" in note and "recalibrate" in note


def test_a_thresholds_file_that_is_not_link_thresholds_is_a_value_error(tmp_path):
    path = tmp_path / "links.json"
    for text in ('[1, 2]', '{"rx1<-00": {"x": 1}}', '{"thresholds": {"rx1<-00": 3.0}, "scorer": {"window_s": 1}}'):
        path.write_text(text)
        with pytest.raises(ValueError):
            read_thresholds(path)


def row_at(label, state, pkt_rate):
    return {"id": label, "state": state, "pkt_rate": pkt_rate}


def test_rate_notes_flag_links_whose_packet_rate_moved_more_than_30_percent():
    rates = {"rx1<-00": 40.0, "rx1<-01": 40.0, "rx2<-00": 40.0}
    rows = [row_at("rx1<-00", "EMPTY", 24.0),      # 40% lower: flagged
            row_at("rx1<-01", "MOTION", 46.0),     # 15% higher: fine
            row_at("rx2<-00", "WARMUP", 10.0),     # still warming up: not judged
            row_at("rx2<-01", "EMPTY", 90.0)]      # no recorded rate: not judged
    (note,) = rate_notes(rates, rows)
    assert note.startswith("rx1<-00") and "24 pkt/s" in note and "40 pkt/s" in note


def test_every_link_scores_with_the_live_scorer():
    tagged = empty_links(seconds=4, seed=52)
    monitor, _, end = run_monitor(tagged, {})
    label = "rx1<-00"
    scorer = MotionScorer.from_config(LIVE_SCORER)
    expected = None
    for receiver, pkt in tagged:
        if LinkId(receiver, pkt.mac).label == label:
            expected = scorer.push(pkt)
    rows = {row["id"]: row for row in monitor.snapshot(now=end)}
    assert rows[label]["score"] == pytest.approx(expected, rel=1e-12)
