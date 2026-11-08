"""Link health in LinkMonitor snapshot rows: seq_loss, seq_rate, clip_pct, agc_gain, fft_gain and noise_floor.

The dashboard passes these six keys through to each link in its payload; each is None when unknown.
"""
import pytest

from csi.links import NO_SIGNAL, LinkId, LinkMonitor
from csi.parser import CSIParser
from csi.simulate import SENDER_MACS, Segment, simulate_links

HEALTH_KEYS = ("seq_loss", "seq_rate", "clip_pct", "agc_gain", "fft_gain", "noise_floor")
T0 = 1_700_000_000.0
MAC = "1a:0c:12:00:00:01"


def c5_line(seq: int, device_us: int, rails: int, agc: int = 30, fft: int = 2, noise_floor: int = -97) -> str:
    """A C5 CSI_DATA line (234 raw values, so the layout locks at once) with ``rails`` values at the int8 rail."""
    values = [127] * rails + [5] * (234 - rails)
    return (f"CSI_DATA,{seq},{MAC},-40,11,{noise_floor},{fft},{agc},6,{device_us},47,2,234,0,"
            f'"[{",".join(map(str, values))}]"')


def rows_by_id(monitor: LinkMonitor, now: float) -> dict:
    return {row["id"]: row for row in monitor.snapshot(now=now)}


def test_every_row_has_the_health_keys_and_a_clean_simulated_link_shows_no_loss():
    tagged = list(simulate_links(senders=SENDER_MACS, receivers=("rx1", "rx2"), scenario=[Segment("empty", 5)], seed=3))
    monitor = LinkMonitor({})
    for receiver, pkt in tagged:
        monitor.push(receiver, pkt)
    rows = monitor.snapshot(now=tagged[-1][1].host_ts)
    assert len(rows) == 4
    for row in rows:
        assert all(key in row for key in HEALTH_KEYS), row["id"]
        assert row["seq_loss"] == 0.0
        assert row["seq_rate"] == pytest.approx(100.0, rel=1e-3)
        assert row["clip_pct"] == 0.0                     # simulated I/Q stays well inside the int8 range
        assert row["agc_gain"] is None and row["fft_gain"] is None   # the S3/ESP32 layout prints no gains


def test_c5_link_reports_seq_loss_rate_clipping_and_receiver_gains():
    parser, monitor = CSIParser(), LinkMonitor({})
    rate_hz = 1000 / 26                                    # a "40 Hz" sender really sends 38.46 Hz
    for i in range(400):
        if i % 5 == 2:
            continue                                       # one packet in five never arrives
        t = T0 + i / rate_hz
        pkt = parser.parse_line(c5_line(seq=1000 + i, device_us=round(i * 26_000), rails=23, agc=30 + i % 2),
                                host_ts=t)
        monitor.push("rx1", pkt)
    row = rows_by_id(monitor, t)[LinkId("rx1", MAC).label]
    assert row["seq_loss"] == pytest.approx(0.2, abs=0.01)
    assert row["seq_rate"] == pytest.approx(rate_hz, rel=1e-3)
    assert row["clip_pct"] == pytest.approx(23 / 234)
    assert (row["agc_gain"], row["fft_gain"], row["noise_floor"]) == (31, 2, -97)   # from the newest packet


def test_a_silent_link_has_no_windowed_health_but_keeps_its_last_gains():
    parser, monitor = CSIParser(), LinkMonitor({})
    for i in range(100):
        t = T0 + i / 100
        monitor.push("rx1", parser.parse_line(c5_line(seq=i, device_us=i * 10_000, rails=0), host_ts=t))
    row = rows_by_id(monitor, t + 5.0)[LinkId("rx1", MAC).label]
    assert row["state"] == NO_SIGNAL
    assert row["seq_loss"] is None and row["seq_rate"] is None and row["clip_pct"] is None
    assert (row["agc_gain"], row["fft_gain"], row["noise_floor"]) == (30, 2, -97)


def test_an_expected_link_that_never_sent_has_every_health_key_none():
    monitor = LinkMonitor({}, expected=[LinkId("rx3", MAC)])
    row, = monitor.snapshot(now=T0)
    assert all(row[key] is None for key in HEALTH_KEYS)
