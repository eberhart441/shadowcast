"""Recordings carry the sender MAC in a `mac` column; files written before it existed still load."""
import json

import pandas as pd

from csi.recording import load_session, record_session
from csi.simulate import NULL_SUBCARRIERS, N_SUBCARRIERS, SENDER_MACS, Segment, simulate_stream


def test_recording_writes_mac_column_after_channel(tmp_path):
    record_session(simulate_stream(scenario=(Segment("empty", 3),), rate_hz=100.0), tmp_path / "s",
                   "empty", send_rate_hz=100.0, countdown_s=0.0)
    (path,) = (tmp_path / "s").glob("*.csv")
    df = pd.read_csv(path)
    n_kept = N_SUBCARRIERS - len(NULL_SUBCARRIERS)
    assert list(df.columns[:5]) == ["host_ts", "device_ts", "rssi", "channel", "mac"]
    assert list(df.columns[5:]) == [f"amp_{i}" for i in range(n_kept)]
    assert set(df["mac"]) == {SENDER_MACS[0]}


def test_loaded_packets_carry_the_recorded_mac(tmp_path):
    stream = simulate_stream(scenario=(Segment("empty", 2),), rate_hz=100.0)
    record_session(stream, tmp_path / "s", "empty", send_rate_hz=100.0, countdown_s=0.0)
    (rec,) = load_session(tmp_path / "s").recordings
    assert {p.mac for p in rec.packets()} == {SENDER_MACS[0]}


def test_old_recording_without_mac_column_still_loads(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "empty_20231114T221320Z.csv").write_text(
        "host_ts,device_ts,rssi,channel,amp_0,amp_1\n"
        "1700000000.000000,1000,-40,6,3.000,4.000\n"
        "1700000000.010000,2000,-41,6,5.000,6.000\n")
    (folder / "session.json").write_text(json.dumps({"session": "old", "channel": 6}))
    (rec,) = load_session(folder).recordings
    assert len(rec.table) == 2
    assert [p.mac for p in rec.packets()] == ["", ""]
    assert rec.amplitude.shape == (2, 2)
