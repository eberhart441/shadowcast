import numpy as np

from csi.recording import load_session, record_session
from csi.simulate import Segment, simulate_stream


def test_round_trip_loads_label_packets_and_meta(tmp_path):
    s = simulate_stream(scenario=(Segment("still", 5),), rate_hz=100.0)
    record_session(s, tmp_path / "s", "still_standing", send_rate_hz=100.0, countdown_s=0.0)
    session = load_session(tmp_path / "s")
    (rec,) = session.recordings
    assert rec.label == "still_standing" and session.meta["channel"] == 6
    assert len(rec.table) == 500
    pkts = rec.packets()
    assert len(pkts) == 500 and pkts[0].amplitude.shape == (rec.amplitude.shape[1],)
    assert np.all(np.diff(rec.host_ts) > 0)
