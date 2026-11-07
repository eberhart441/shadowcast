import pytest

from csi.health import LinkHealth
from csi.parser import CSIParser
from csi.simulate import Segment, simulate_packets, simulate_stream

EMPTY = (Segment("empty", 30),)


def run(stream, send_rate_hz=100.0, **kw):
    health = LinkHealth(send_rate_hz=send_rate_hz, **kw)
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


# --- device counter events: reboot/wrap must not freeze the drop count or invent drops ---

WRAP = 2 ** 32


def test_reboot_does_not_count_drops_and_drops_keep_counting_afterwards():
    reboot_us = 10_000_000
    stream = simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=3, drop_prob=0.1,
                             device_clock=lambda t: t if t < reboot_us else t - reboot_us)
    true_drops = 3000 - len(list(simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=3, drop_prob=0.1)))
    health, _, _ = run(stream)
    assert health.drops == pytest.approx(true_drops, abs=0.1 * true_drops)
    assert health.drops > 200  # drops after the reboot are still counted (old code froze at 0)


def test_reboot_without_drops_reports_zero():
    reboot_us = 5_000_000
    health, _, n = run(simulate_stream(scenario=EMPTY, rate_hz=100.0,
                                       device_clock=lambda t: t if t < reboot_us else t - reboot_us))
    assert n == 3000 and health.drops == 0


def test_wrap_keeps_drop_counting_going():
    start = WRAP - 4_000_000
    health, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=4, drop_prob=0.1,
                                       device_clock=lambda t: (t + start) % WRAP))
    plain, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=100.0, seed=4, drop_prob=0.1))
    assert health.drops == plain.drops > 200


def test_out_of_order_host_timestamps_do_not_skew_packet_rate():
    lines = list(simulate_stream(scenario=EMPTY, rate_hz=100.0))
    shuffled = [(ts - 5.0 if i % 10 == 0 else ts, ln) for i, (ts, ln) in enumerate(lines)]
    health, _, _ = run(iter(shuffled), window_s=2.0)
    assert health.packet_rate() == pytest.approx(100.0, rel=0.1)


def test_auto_rate_follows_a_non_100hz_sender_without_false_drops():
    health, _, n = run(simulate_stream(scenario=EMPTY, rate_hz=50.0), send_rate_hz=None)
    assert n == 1500 and health.drops == 0
    assert health.rate_hz() == pytest.approx(50.0, rel=0.02)


def test_fixed_100hz_assumption_mis_counts_a_50hz_sender():
    health, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=50.0), send_rate_hz=100.0)
    assert health.drops > 1000  # why real ports must not assume the simulator rate


def test_auto_rate_is_none_until_enough_packets():
    health, _, _ = run(simulate_stream(scenario=(Segment("empty", 0.2),), rate_hz=100.0), send_rate_hz=None)
    assert health.rate_hz() is None
