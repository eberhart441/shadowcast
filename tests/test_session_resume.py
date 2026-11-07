"""Re-opening a session folder, per-file settings, and loading folders with stray CSV files."""
import json

import pytest

from csi.parser import DEFAULT_HEADER
from csi.recording import load_session, record_session
from csi.simulate import NULL_SUBCARRIERS, N_SUBCARRIERS, Segment, simulate_stream


def stream(seconds=3, **kw):
    return simulate_stream(scenario=(Segment("empty", seconds),), rate_hz=100.0, **kw)


def csi_line(ts, values):
    """One esp-csi CSI_DATA line with an arbitrary subcarrier count (values are I/Q ints)."""
    cols = DEFAULT_HEADER.split(",")[:-1]
    v = {"type": "CSI_DATA", "rssi": -40, "channel": 6, "local_timestamp": ts, "len": len(values)}
    return ",".join(str(v.get(c, 0)) for c in cols) + f',"[{",".join(map(str, values))}]"'


def narrow_stream(seconds=5, n_sub=10):
    """A stream whose packets carry a different subcarrier count than the simulator's 64."""
    lines = [csi_line(i * 10_000, [1 + (i + k) % 5 for k in range(2 * n_sub)])
             for i in range(int(seconds * 100))]
    return [(1_700_000_000.0 + i * 0.01, ln) for i, ln in enumerate(lines)]


def test_resumed_session_keeps_prior_files_drops_and_per_file_settings(tmp_path):
    folder = tmp_path / "sess"
    first = record_session(stream(3, drop_prob=0.1, seed=3), folder, "empty",
                           send_rate_hz=100.0, countdown_s=0.0, placement_notes="tx left")
    second = record_session(stream(3, seed=4), folder, "walking", send_rate_hz=50.0,
                            countdown_s=0.0, placement_notes="tx right")
    meta = json.loads((folder / "session.json").read_text())
    assert [f["label"] for f in meta["files"]] == ["empty", "walking"]
    assert meta["drops_estimated"] == first.drops + second.drops and first.drops > 0
    assert meta["channel"] == 6
    assert meta["subcarrier_layout"]["n_subcarriers"] == N_SUBCARRIERS - len(NULL_SUBCARRIERS)
    assert "tx left" in meta["placement_notes"] and "tx right" in meta["placement_notes"]
    walking = meta["files"][1]
    assert walking["channel"] == 6 and walking["send_rate_hz"] == 50.0
    assert walking["subcarrier_layout"]["n_subcarriers"] == N_SUBCARRIERS - len(NULL_SUBCARRIERS)
    assert meta["files"][0]["send_rate_hz"] == 100.0


def test_resume_with_a_different_subcarrier_count_is_refused_clearly(tmp_path):
    folder = tmp_path / "sess"
    record_session(stream(3), folder, "empty", send_rate_hz=100.0, countdown_s=0.0)
    before = json.loads((folder / "session.json").read_text())
    with pytest.raises(ValueError, match="subcarriers.*new session"):
        record_session(narrow_stream(), folder, "walking", send_rate_hz=100.0, countdown_s=0.0)
    assert len(list(folder.glob("*.csv"))) == 1
    after = json.loads((folder / "session.json").read_text())
    assert after["files"] == before["files"]


def test_rate_is_estimated_from_device_timestamps_when_not_given(tmp_path):
    record_session(simulate_stream(scenario=(Segment("empty", 4),), rate_hz=50.0), tmp_path / "s",
                   "empty", countdown_s=0.0)  # no send_rate_hz: the real-port behaviour
    meta = json.loads((tmp_path / "s" / "session.json").read_text())
    assert meta["send_rate_hz"] == pytest.approx(50.0, rel=0.02)
    assert meta["files"][0]["send_rate_hz"] == pytest.approx(50.0, rel=0.02)
    assert meta["drops_estimated"] == 0


def test_load_session_skips_csv_files_that_are_not_recordings(tmp_path):
    folder = tmp_path / "s"
    record_session(stream(3), folder, "empty", send_rate_hz=100.0, countdown_s=0.0)
    (folder / "notes.csv").write_text("when,what\n1,2\n")
    (folder / "running_20260101T000000Z.csv").write_text("host_ts\n1\n")
    session = load_session(folder)
    assert [r.label for r in session.recordings] == ["empty"]
