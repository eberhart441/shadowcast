import numpy as np
import pytest

from csi.parser import DEFAULT_HEADER, WARMUP_PACKETS, CSIParser, parse_header, parse_stream
from csi.simulate import N_SUBCARRIERS, NULL_SUBCARRIERS, Segment, simulate_packets, simulate_stream


def line(rssi=-40, channel=6, ts=1000, values=None, length=None, **columns):
    """A CSI_DATA line in the S3/ESP32 (DEFAULT_HEADER) layout; ``columns`` sets other columns by name (default 0)."""
    values = values if values is not None else [3, 4, 0, 0, 6, 8]
    cols = DEFAULT_HEADER.split(",")[:-1]
    v = {"type": "CSI_DATA", "rssi": rssi, "channel": channel, "local_timestamp": ts,
         "len": length if length is not None else len(values), **columns}
    prefix = ",".join(str(v.get(c, 0)) for c in cols)
    return f'{prefix},"[{",".join(map(str, values))}]"'


def warm_up(ps, text=None):
    """Lock an unknown-length layout: WARMUP_PACKETS packets of one raw length."""
    for _ in range(WARMUP_PACKETS):
        ps.parse_line(text or line(), host_ts=0.0)


def test_amplitude_and_null_drop():
    ps = CSIParser()
    warm_up(ps)
    p = ps.parse_line(line(), host_ts=5.0)
    assert p.host_ts == 5.0 and p.device_ts == 1000 and p.rssi == -40 and p.channel == 6
    np.testing.assert_allclose(p.amplitude, [5.0, 10.0])  # middle subcarrier (0,0) dropped


def test_header_maps_columns_not_indices():
    hdr = "type,local_timestamp,channel,rssi,len,data"
    assert parse_header(hdr) is not None
    ps = CSIParser()
    assert ps.parse_line(hdr) is None
    text = 'CSI_DATA,777,11,-55,4,"[3,4,6,8]"'
    warm_up(ps, text)
    pkt = ps.parse_line(text)
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
    warm_up(ps)
    assert ps.parse_line(bad) is None
    assert ps.stats.malformed == 1
    assert ps.parse_line(line()) is not None  # parser still works afterwards


def test_non_csi_lines_ignored():
    ps = CSIParser()
    for s in ["", "I (123) wifi: connected", "ets Jun  8 2016"]:
        assert ps.parse_line(s) is None
    assert ps.stats.ignored == 3 and ps.stats.malformed == 0


def test_layout_locked_after_warm_up():
    ps = CSIParser()
    warm_up(ps)
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


C5_LINE = ('CSI_DATA,3320,1a:00:00:00:00:00,-5,11,-97,-10,0,6,33253679,47,2,8,0,'
           '"[18,6,18,8,17,13,0,0]"')


def test_headerless_c5_layout_is_inferred_from_column_count():
    # Real esp32-c5 csi_recv line shape (15 columns); header was missed at boot.
    ps = CSIParser()
    warm_up(ps, C5_LINE)
    pkt = ps.parse_line(C5_LINE, host_ts=1.0)
    assert (pkt.rssi, pkt.channel, pkt.device_ts) == (-5, 6, 33253679)
    assert len(pkt.amplitude) == 3 and ps.stats.malformed == 0


def test_c5_mask_is_stable_when_first_packet_is_mostly_zero():
    # Low-gain C5 packets are mostly 0/+-1; the kept layout must not depend on the first packet.
    ps = CSIParser()
    sparse = [0, 0] * 117
    sparse[10], sparse[11] = 1, 0
    busy = [1, 1] * 117
    a = ps.parse_line(line(values=sparse), host_ts=0.0)
    b = ps.parse_line(line(values=busy), host_ts=0.1)
    assert len(a.amplitude) == len(b.amplitude) == 114


# --- unknown raw lengths: the null mask comes from a warm-up, and the layout can re-lock ---

def feed_all(ps, lines, host_ts=0.0):
    out = []
    for i, ln in enumerate(lines):
        out.extend(ps.feed(ln, host_ts + i * 0.01))
    return out


def test_mask_is_union_over_warm_up_not_the_first_packet():
    # subcarrier 1 is zero in the first packet only (low gain); it must still be kept
    ps = CSIParser()
    first = line(values=[3, 4, 0, 0, 6, 8])
    later = [line(values=[3, 4, 1, 1, 6, 8])] * 19
    out = feed_all(ps, [first, *later])
    assert len(out) == 20
    assert ps.kept_subcarriers == [0, 1, 2]
    assert all(len(p.amplitude) == 3 for p in out)


def test_warm_up_packets_are_emitted_in_order_once_locked():
    ps = CSIParser()
    lines = [line(ts=1000 + i, values=[3, 4, 1, 1, 6, 8]) for i in range(30)]
    out = feed_all(ps, lines)
    assert [p.device_ts for p in out] == [1000 + i for i in range(30)]


def test_relock_when_most_packets_switch_to_a_new_length():
    ps = CSIParser()
    feed_all(ps, [line(values=[3, 4, 1, 1, 6, 8])] * 20)          # locks 3 subcarriers
    new = [line(values=[1, 1, 2, 2, 3, 3, 4, 4])] * 80             # now 4 subcarriers
    out = feed_all(ps, new)
    assert ps.stats.relocks == 1
    assert out and all(len(p.amplitude) == 4 for p in out)
    assert ps.kept_subcarriers == [0, 1, 2, 3]


def test_a_single_odd_length_packet_does_not_relock():
    ps = CSIParser()
    feed_all(ps, [line(values=[3, 4, 1, 1, 6, 8])] * 20)
    out = feed_all(ps, [line(values=[1, 1, 2, 2, 3, 3, 4, 4])] + [line(values=[3, 4, 1, 1, 6, 8])] * 5)
    assert ps.stats.relocks == 0 and ps.stats.layout_skipped == 1
    assert len(out) == 5 and all(len(p.amplitude) == 3 for p in out)


def test_known_c5_length_locks_immediately_without_warm_up():
    ps = CSIParser()
    pkt = ps.parse_line(line(values=[1] * 234), host_ts=1.0)  # 117 subcarriers, 3 known nulls
    assert pkt is not None and len(pkt.amplitude) == 114 and ps.stats.lines == 1


# --- health fields: the sender's packet counter, receiver gains, noise floor, clipping ---

def test_c5_line_carries_the_counter_gains_noise_floor_and_raw_count():
    ps = CSIParser()
    warm_up(ps, C5_LINE)
    pkt = ps.parse_line(C5_LINE, host_ts=1.0)
    # C5_HEADER order: seq, mac, rssi, rate, noise_floor, fft_gain, agc_gain, ...
    assert (pkt.seq, pkt.noise_floor, pkt.fft_gain, pkt.agc_gain) == (3320, -97, -10, 0)
    assert (pkt.n_raw, pkt.clipped) == (8, 0)


def test_s3_layout_reads_the_counter_from_id_and_prints_no_gains():
    ps = CSIParser()
    warm_up(ps)
    pkt = ps.parse_line(line(id=41, noise_floor=-92), host_ts=1.0)
    assert (pkt.seq, pkt.noise_floor) == (41, -92)
    assert pkt.agc_gain is None and pkt.fft_gain is None


def test_a_counter_printed_negative_reads_back_as_uint32():
    # the firmware prints the uint32 counter with %d: past 2**31 it reads negative
    ps = CSIParser()
    warm_up(ps)
    assert ps.parse_line(line(id=-2147483648), host_ts=1.0).seq == 2 ** 31
    assert ps.parse_line(line(id=-1), host_ts=1.0).seq == 2 ** 32 - 1


def test_a_bad_value_in_a_health_column_gives_none_and_the_line_still_parses():
    ps = CSIParser()
    warm_up(ps)
    pkt = ps.parse_line(line(id="x", noise_floor="nan"), host_ts=1.0)
    assert pkt is not None and ps.stats.malformed == 0
    assert pkt.seq is None and pkt.noise_floor is None


def test_a_header_without_health_columns_gives_none():
    ps = CSIParser()
    ps.parse_line("type,local_timestamp,channel,rssi,len,data")
    text = 'CSI_DATA,777,11,-55,4,"[3,4,6,8]"'
    warm_up(ps, text)
    pkt = ps.parse_line(text, host_ts=1.0)
    assert (pkt.seq, pkt.agc_gain, pkt.fft_gain, pkt.noise_floor) == (None, None, None, None)
    assert (pkt.n_raw, pkt.clipped) == (4, 0)


def test_clipped_counts_raw_values_at_the_int8_rails():
    ps = CSIParser()
    warm_up(ps)
    pkt = ps.parse_line(line(values=[127, -128, -127, 126, 0, 5]), host_ts=1.0)
    assert (pkt.clipped, pkt.n_raw) == (3, 6)


def test_simulated_lines_carry_a_contiguous_counter():
    pkts = list(parse_stream(simulate_stream(scenario=[Segment("empty", 1)], seed=1)))
    assert [p.seq for p in pkts] == list(range(100))
