import numpy as np
import pytest

from csi.parser import DEFAULT_HEADER, CSIParser, parse_header, parse_stream
from csi.simulate import N_SUBCARRIERS, NULL_SUBCARRIERS, Segment, simulate_packets, simulate_stream


def line(rssi=-40, channel=6, ts=1000, values=None, length=None):
    values = values if values is not None else [3, 4, 0, 0, 6, 8]
    cols = DEFAULT_HEADER.split(",")[:-1]
    v = {"type": "CSI_DATA", "rssi": rssi, "channel": channel, "local_timestamp": ts,
         "len": length if length is not None else len(values)}
    prefix = ",".join(str(v.get(c, 0)) for c in cols)
    return f'{prefix},"[{",".join(map(str, values))}]"'


def test_amplitude_and_null_drop():
    p = CSIParser().parse_line(line(), host_ts=5.0)
    assert p.host_ts == 5.0 and p.device_ts == 1000 and p.rssi == -40 and p.channel == 6
    np.testing.assert_allclose(p.amplitude, [5.0, 10.0])  # middle subcarrier (0,0) dropped


def test_header_maps_columns_not_indices():
    hdr = "type,local_timestamp,channel,rssi,len,data"
    assert parse_header(hdr) is not None
    ps = CSIParser()
    assert ps.parse_line(hdr) is None
    pkt = ps.parse_line('CSI_DATA,777,11,-55,4,"[3,4,6,8]"')
    assert (pkt.device_ts, pkt.channel, pkt.rssi) == (777, 11, -55)


@pytest.mark.parametrize("bad", [
    "CSI_DATA,1,2",                                   # truncated
    line()[:-8],                                      # partial: no closing bracket
    line(values=[1, 2, 3]),                           # odd count
    line(values=[1, 2, 300, 4]),                      # out of int8 range
    line(length=99),                                  # len disagrees
    line().replace("-40", "abc"),                     # non-numeric rssi
    'CSI_DATA,"[1,2]"',                               # wrong column count
])
def test_malformed_skipped_and_counted(bad):
    ps = CSIParser()
    assert ps.parse_line(bad) is None
    assert ps.stats.malformed == 1
    assert ps.parse_line(line()) is not None  # parser still works afterwards


def test_non_csi_lines_ignored():
    ps = CSIParser()
    for s in ["", "I (123) wifi: connected", "ets Jun  8 2016"]:
        assert ps.parse_line(s) is None
    assert ps.stats.ignored == 3 and ps.stats.malformed == 0


def test_layout_locked_to_first_packet():
    ps = CSIParser()
    assert len(ps.parse_line(line()).amplitude) == 2
    assert ps.parse_line(line(values=[1, 1, 2, 2, 3, 3, 4, 4])) is None
    assert ps.stats.layout_skipped == 1


def test_simulator_roundtrip_through_parser():
    ps = CSIParser()
    pkts = list(parse_stream(simulate_stream(scenario=[Segment("empty", 2)], seed=1), ps))
    assert len(pkts) == 200 and ps.stats.malformed == 0
    assert len(pkts[0].amplitude) == N_SUBCARRIERS - len(NULL_SUBCARRIERS)
    assert pkts[1].host_ts - pkts[0].host_ts == pytest.approx(0.01)


def test_simulator_states_differ():
    def var(state):
        a = np.array([p.amplitude for p, _ in simulate_packets([Segment(state, 10)], seed=2)])
        return a.var(axis=0).mean()
    assert var("motion") > 5 * var("empty")


def test_simulator_breathing_peak_near_0p3hz():
    a = np.array([p.amplitude for p, _ in simulate_packets([Segment("still", 30)], seed=3)])
    x = a[:, a.var(axis=0).argmax()]
    x = x - x.mean()
    freqs = np.fft.rfftfreq(len(x), 0.01)
    mask = (freqs > 0.1) & (freqs < 0.5)
    assert freqs[mask][np.abs(np.fft.rfft(x))[mask].argmax()] == pytest.approx(0.3, abs=0.05)


def test_simulator_drops_visible_in_device_ts():
    pk = [p for p, _ in simulate_packets([Segment("empty", 5)], drop_prob=0.2, seed=4)]
    assert 350 < len(pk) < 450
    assert np.diff([p.device_ts for p in pk]).max() > 15_000  # >1 interval gap
