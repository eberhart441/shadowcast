import pytest

from csi.health import LinkHealth
from csi.parser import CSIParser
from csi.simulate import Segment, simulate_packets, simulate_stream

EMPTY = (Segment("empty", 30),)


def run(stream, **kw):
    health = LinkHealth(send_rate_hz=100.0, **kw)
    parser = CSIParser()
    n = sum(1 for _ in health.consume(stream, parser))
    return health, parser, n


def test_packet_rate_over_recent_window():
    health, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=100.0), window_s=2.0)
    assert health.packet_rate() == pytest.approx(100.0, rel=0.05)


def test_drop_free_stream_reports_zero_drops():
    health, _, n = run(simulate_stream(scenario=EMPTY, rate_hz=100.0))
    assert n == 3000 and health.drops == 0


def test_drops_estimated_close_to_truth():
    health, _, n = run(simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=3, drop_prob=0.1))
    true_drops = 3000 - n
    assert true_drops > 100
    assert abs(health.drops - true_drops) <= 0.1 * true_drops


def test_rate_reflects_drops():
    health, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=3, drop_prob=0.5),
                       window_s=5.0)
    assert health.packet_rate() == pytest.approx(50.0, rel=0.2)


def test_malformed_and_out_of_order_do_not_crash_or_inflate_drops():
    lines = list(simulate_stream(scenario=(Segment("empty", 5),), rate_hz=100.0))
    noisy = []
    for i, (ts, ln) in enumerate(lines):
        noisy.append((ts, ln))
        if i % 50 == 10:
            noisy.append((ts, ln[:-9]))          # partial line
            noisy.append((ts, "garbage"))
        if i % 70 == 20:
            noisy.append((ts, ln))               # repeated device timestamp
    noisy[100:102] = noisy[100:102][::-1]        # out-of-order pair
    health, parser, _ = run(iter(noisy))
    assert parser.stats.malformed > 0
    assert health.drops <= 2


def test_empty_stream_is_safe():
    health, _, n = run(iter([]))
    assert n == 0 and health.drops == 0 and health.packet_rate() == 0.0
