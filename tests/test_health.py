import numpy as np
import pytest

from csi.health import SEQ_MIN_PACKETS, LinkHealth
from csi.parser import CSIPacket, CSIParser
from csi.simulate import Segment, simulate_stream

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


# --- loss from the sender's packet counter (seq): needs no nominal rate ---

T0 = 1000.0


def packet(seq, t, **fields):
    """A packet at host time ``t`` (device time follows it) carrying sender counter ``seq``."""
    return CSIPacket(host_ts=t, device_ts=int(t * 1e6), rssi=-40, channel=6, amplitude=np.zeros(2, np.float32),
                     seq=seq, **fields)


def sender(rate_hz, seconds, start=0, keep=lambda i: True, t0=T0):
    """A sender at ``rate_hz``: counter start + i arrives at t0 + i / rate_hz, for each i that ``keep`` lets through."""
    return [packet((start + i) % 2 ** 32, t0 + i / rate_hz) for i in range(round(seconds * rate_hz)) if keep(i)]


def observed(packets, **kw):
    health = LinkHealth(**kw)
    for p in packets:
        health.observe(p)
    return health


def every_tenth_lost(i):
    return i % 10 != 3


def test_seq_loss_and_rate_follow_the_real_sender_whatever_nominal_rate_is_assumed():
    # a sender built for "100 Hz" really sends 90.9 Hz; one packet in ten is lost
    packets = sender(1000 / 11, 30, keep=every_tenth_lost)
    for nominal in (100.0, None):
        health = observed(packets, send_rate_hz=nominal)
        assert health.seq_loss() == pytest.approx(0.1, abs=0.005)
        assert health.seq_rate() == pytest.approx(1000 / 11, rel=1e-3)


def test_seq_loss_is_none_until_enough_packets():
    health = observed(sender(100.0, (SEQ_MIN_PACKETS - 1) / 100.0))
    assert health.seq_loss() is None and health.seq_rate() is None
    health.observe(packet(SEQ_MIN_PACKETS - 1, T0 + (SEQ_MIN_PACKETS - 1) / 100.0))
    assert health.seq_loss() == 0.0


def test_seq_loss_covers_only_the_recent_window():
    outage = sender(100.0, 30, keep=lambda i: not 2200 <= i < 2500)   # 3 s lost, 5 to 8 s before the end
    assert observed(outage).seq_loss() == pytest.approx(0.3, abs=0.01)
    later = sender(100.0, 40, keep=lambda i: not 2200 <= i < 2500)    # the same outage, 15 to 18 s before the end
    assert observed(later).seq_loss() == 0.0


def test_a_counter_wrap_is_unwrapped_not_taken_for_a_reboot():
    wrapped = observed(sender(100.0, 20, start=2 ** 32 - 700, keep=every_tenth_lost))
    plain = observed(sender(100.0, 20, keep=every_tenth_lost))
    assert wrapped.seq_restarts == 0
    assert wrapped.seq_loss() == pytest.approx(plain.seq_loss())
    assert plain.seq_loss() == pytest.approx(0.1, abs=0.005)


def test_a_sender_reboot_starts_the_window_over():
    before = sender(100.0, 20, start=5000)
    t_boot = before[-1].host_ts + 1.0
    health = observed(before + sender(100.0, 0.1, t0=t_boot))      # counter back to 0
    assert health.seq_restarts == 1
    assert health.seq_loss() is None                                # only 10 packets since the reboot
    health = observed(before + sender(100.0, 15, t0=t_boot))
    assert health.seq_loss() == 0.0                                 # the reboot itself is not loss
    assert health.seq_rate() == pytest.approx(100.0, rel=1e-3)


def test_repeated_and_late_packets_do_not_change_seq_loss():
    clean = sender(100.0, 20, keep=every_tenth_lost)
    late = clean[:500] + [clean[501], clean[500]] + clean[502:]     # one packet arrives after the next one
    noisy = []
    for p in late:
        noisy.append(p)
        if p.seq % 7 == 0:
            noisy.append(p)                                         # the same packet twice
    health = observed(noisy)
    assert health.seq_loss() == pytest.approx(observed(clean).seq_loss())
    assert health.seq_repeats == sum(1 for p in clean if p.seq % 7 == 0)


def test_packets_without_a_counter_leave_seq_loss_unknown_and_drops_unchanged():
    no_seq = [CSIPacket(host_ts=p.host_ts, device_ts=p.device_ts, rssi=-40, channel=6, amplitude=p.amplitude)
              for p in sender(100.0, 5, keep=every_tenth_lost)]
    health = observed(no_seq, send_rate_hz=100.0)
    assert health.seq_loss() is None and health.seq_rate() is None
    assert health.drops == observed(sender(100.0, 5, keep=every_tenth_lost), send_rate_hz=100.0).drops > 0


def test_a_simulated_stream_shows_no_seq_loss_at_its_rate():
    health, _, _ = run(simulate_stream(scenario=EMPTY, rate_hz=100.0))
    assert health.seq_loss() == 0.0
    assert health.seq_rate() == pytest.approx(100.0, rel=1e-3)


# --- signal conditions: clipping, receiver gains, noise floor ---

def test_clip_fraction_covers_the_last_five_seconds():
    quiet = [packet(i, T0 + i / 100, clipped=0, n_raw=234) for i in range(1000)]
    loud = [packet(1000 + i, T0 + 10 + i / 100, clipped=117, n_raw=234) for i in range(600)]
    assert observed(quiet + loud[:200]).clip_fraction() == pytest.approx(0.2, abs=0.01)    # 2 of the last 5 s
    assert observed(quiet + loud).clip_fraction() == pytest.approx(0.5)


def test_clip_fraction_is_none_without_raw_values():
    assert observed(sender(100.0, 2)).clip_fraction() is None       # e.g. packets loaded from a recording


def test_the_newest_gains_and_noise_floor_are_kept():
    health = observed([packet(0, T0, agc_gain=30, fft_gain=2, noise_floor=-97),
                       packet(1, T0 + 0.01, agc_gain=31, fft_gain=-1, noise_floor=-95)])
    assert (health.agc_gain, health.fft_gain, health.noise_floor) == (31, -1, -95)
