import json
import re

import pandas as pd
import pytest

from csi.recording import LABELS, record_session
from csi.simulate import NULL_SUBCARRIERS, N_SUBCARRIERS, Segment, simulate_stream


def stream(seconds=10, **kw):
    return simulate_stream(scenario=(Segment("empty", seconds),), rate_hz=100.0, **kw)


def record(tmp_path, s=None, label="empty", **kw):
    kw.setdefault("countdown_s", 0.0)
    return record_session(s if s is not None else stream(), tmp_path / "sess", label,
                          send_rate_hz=100.0, **kw)


def switch_at(n, label):
    """Label-switch poll that fires once, on its n-th call (stands in for a keypress)."""
    calls = {"n": 0}

    def poll():
        calls["n"] += 1
        return label if calls["n"] == n else None

    return poll


def test_simulated_recording_writes_named_file_and_metadata(tmp_path):
    report = record(tmp_path, placement_notes="tx left, rx right")
    files = sorted(p.name for p in (tmp_path / "sess").glob("*.csv"))
    assert len(files) == 1 and re.fullmatch(r"empty_\d{8}T\d{6}Z\.csv", files[0])
    meta = json.loads((tmp_path / "sess" / "session.json").read_text())
    assert meta["placement_notes"] == "tx left, rx right"
    assert meta["channel"] == 6 and meta["send_rate_hz"] == 100.0
    assert meta["date"].startswith("2023-11-1")
    kept = [i for i in range(N_SUBCARRIERS) if i not in NULL_SUBCARRIERS]
    assert meta["subcarrier_layout"]["kept_subcarriers"] == kept
    assert report.packets == 1000


def test_file_is_flat_table_with_documented_columns(tmp_path):
    record(tmp_path)
    (path,) = (tmp_path / "sess").glob("*.csv")
    df = pd.read_csv(path)
    n_kept = N_SUBCARRIERS - len(NULL_SUBCARRIERS)
    assert list(df.columns[:4]) == ["host_ts", "device_ts", "rssi", "channel"]
    assert list(df.columns[4:]) == [f"amp_{i}" for i in range(n_kept)]
    assert len(df) == 1000 and (df["channel"] == 6).all()


def test_label_switch_starts_new_file_with_one_label_each(tmp_path):
    report = record(tmp_path, poll_label=switch_at(500, "walking"))
    names = sorted(p.name for p in (tmp_path / "sess").glob("*.csv"))
    assert sorted(n.split("_")[0] for n in names) == ["empty", "walking"]
    total = sum(len(pd.read_csv(p)) for p in (tmp_path / "sess").glob("*.csv"))
    assert total == report.packets == 1000
    meta = json.loads((tmp_path / "sess" / "session.json").read_text())
    assert sorted(f["label"] for f in meta["files"]) == ["empty", "walking"]


def test_countdown_precedes_each_recording(tmp_path):
    ticks = []
    report = record(tmp_path, countdown_s=3.0, poll_label=switch_at(600, "walking"),
                    on_countdown=ticks.append)
    assert ticks == [3, 2, 1, 3, 2, 1]
    assert 1000 - report.packets >= 500  # countdown packets are discarded, not recorded


def test_invalid_label_rejected_with_clear_message(tmp_path):
    with pytest.raises(ValueError, match="empty.*walking.*still_standing.*still_sitting_or_lying"):
        record(tmp_path, label="running")
    assert not list((tmp_path / "sess").glob("*.csv"))
    assert LABELS == ("empty", "walking", "still_standing", "still_sitting_or_lying")


def test_invalid_switch_label_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="running"):
        record(tmp_path, poll_label=switch_at(10, "running"))


def test_interrupt_keeps_data_and_report_counts_drops(tmp_path):
    def interrupted():
        for i, item in enumerate(stream(seed=3, drop_prob=0.1)):
            if i == 700:
                raise KeyboardInterrupt
            yield item

    report = record(tmp_path, interrupted())
    (path,) = (tmp_path / "sess").glob("*.csv")
    assert len(pd.read_csv(path)) == report.packets > 500
    assert report.interrupted and report.drops > 20


def test_duration_stops_recording(tmp_path):
    report = record(tmp_path, duration_s=3.0)
    assert 290 <= report.packets <= 310


def test_second_recording_into_same_session_appends_metadata(tmp_path):
    import json
    from csi.recording import record_session
    from csi.simulate import Segment, simulate_stream
    for label in ("empty", "walking"):
        record_session(simulate_stream(scenario=[Segment("empty", 3)], seed=1), tmp_path / "s",
                       label, countdown_s=0)
    meta = json.loads((tmp_path / "s" / "session.json").read_text())
    assert [f["label"] for f in meta["files"]] == ["empty", "walking"]
