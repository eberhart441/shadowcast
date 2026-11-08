"""Wireless receivers: the gateway's one stream is split back into one stream per receiver (csi/gateway.py)."""
import heapq

import pytest

from csi.gateway import split_gateway
from csi.parser import CSIParser, parse_stream
from csi.simulate import simulate_room

SERIALS = {"38:44:BE:A5:18:B0": "rx1", "10:BD:A3:CC:85:BC": "rx2"}


def drain(stream):
    return [line for _, line in stream if line is not None]


def test_lines_go_to_the_receiver_named_by_their_serial_without_the_prefix():
    lines = [(1.0, "RX,38:44:BE:A5:18:B0,CSI_DATA,1"), (2.0, "RX,10:bd:a3:cc:85:bc,CSI_DATA,2"),
             (3.0, "RX,38:44:BE:A5:18:B0,CSI_DATA,3")]
    streams = split_gateway(iter(lines), SERIALS)

    assert drain(streams["rx1"]) == ["CSI_DATA,1", "CSI_DATA,3"]
    assert drain(streams["rx2"]) == ["CSI_DATA,2"]


def test_unknown_boards_boot_noise_and_heartbeats_are_dropped():
    lines = [(1.0, "W (284) csi_recv: CSI gateway on channel 6"), (1.1, None),
             (1.2, "RX,AA:AA:AA:AA:AA:AA,CSI_DATA,9"), (1.3, "RX,38:44:BE:A5:18:B0,CSI_DATA,1")]
    streams = split_gateway(iter(lines), SERIALS)

    assert drain(streams["rx1"]) == ["CSI_DATA,1"]
    assert drain(streams["rx2"]) == []


def test_a_failing_gateway_port_fails_every_receiver():
    def lines():
        yield 1.0, "RX,38:44:BE:A5:18:B0,CSI_DATA,1"
        raise OSError("device disconnected")

    streams = split_gateway(lines(), SERIALS)
    for name in ("rx1", "rx2"):
        with pytest.raises(OSError, match="disconnected"):
            drain(streams[name])


def test_forwarded_receivers_parse_into_the_same_packets_as_wired_ones():
    """Simulated rx1 and rx2 lines, prefixed and merged as the gateway would write them, then split."""
    wired = {name: list(lines) for name, lines in simulate_room(duration_s=3, seed=4).items()}
    serial_of = {name: serial for serial, name in SERIALS.items()}
    merged = heapq.merge(*[[(ts, f"RX,{serial_of[name]},{line}") for ts, line in lines]
                           for name, lines in wired.items()], key=lambda item: item[0])

    streams = split_gateway(merged, SERIALS)

    for name in ("rx1", "rx2"):
        via_gateway = list(parse_stream(streams[name], CSIParser()))
        direct = list(parse_stream(iter(wired[name]), CSIParser()))
        assert len(via_gateway) == len(direct) > 0
        assert [(p.mac, p.device_ts) for p in via_gateway] == [(p.mac, p.device_ts) for p in direct]
