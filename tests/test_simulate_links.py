"""Simulated 2-sender x 2-receiver links, driven through the text seam (one line stream per receiver)."""
import numpy as np

from csi.parser import CSIParser
from csi.simulate import SENDER_MACS, Segment, packet_to_line, simulate_link_streams, simulate_links, simulate_packets

RECEIVERS = ("rx1", "rx2")
EMPTY = (Segment("empty", 5),)


def test_packet_to_line_renders_the_given_mac_and_parser_reads_it_back():
    pkt, _ = next(simulate_packets(scenario=EMPTY, seed=1))
    line = packet_to_line(pkt, idx=0, mac="1A:00:00:00:00:01")
    ps = CSIParser()
    out = None
    for _ in range(25):  # enough identical lines to lock the layout
        out = ps.parse_line(line, host_ts=0.0) or out
    assert out is not None and out.mac == "1a:00:00:00:00:01"


def test_each_receiver_stream_carries_every_sender_mac_in_its_text():
    streams = simulate_link_streams(senders=SENDER_MACS, receivers=RECEIVERS, scenario=EMPTY, seed=2)
    for name in RECEIVERS:
        macs = set()
        for _, line in streams[name]:
            if line.startswith("CSI_DATA"):
                macs.add(line.split(",")[2])
        assert macs == set(SENDER_MACS), name


def test_simulate_links_yields_receiver_and_packet_with_mac_for_each_link():
    got = list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS, scenario=EMPTY, seed=3))
    assert {(r, p.mac) for r, p in got} == {(r, m) for r in RECEIVERS for m in SENDER_MACS}


def test_links_have_independent_noise_and_baselines():
    got = list(simulate_links(senders=SENDER_MACS, receivers=("rx1",), scenario=EMPTY, seed=4))
    by_mac = {}
    for _, p in got:
        by_mac.setdefault(p.mac, []).append(p.amplitude)
    a = np.stack(by_mac[SENDER_MACS[0]][:50])
    b = np.stack(by_mac[SENDER_MACS[1]][:50])
    assert not np.allclose(a, b)


def test_scenario_applies_to_all_links_at_once():
    scenario = (Segment("empty", 2), Segment("motion", 2))
    got = list(simulate_links(senders=SENDER_MACS, receivers=RECEIVERS, scenario=scenario, seed=5))
    per_link = {}
    for r, p in got:
        per_link.setdefault((r, p.mac), []).append(p.host_ts)
    spans = {k: (min(v), max(v)) for k, v in per_link.items()}
    assert len(set(round(v[1] - v[0], 3) for v in spans.values())) == 1  # same duration on every link
    assert len(per_link) == 4
