"""Link health from the packet stream: recent packet rate, loss, and the receiver's signal conditions.

Loss is measured two ways. Trust ``seq_loss()``:

- ``seq_loss()`` and ``seq_rate()`` come from the sender's own packet counter (``CSIPacket.seq``) over the
  last ``seq_window_s`` seconds: the counter values that never arrived are the lost packets, and the
  counter's span over time is the rate the sender really sends at (a "100 Hz" sender sends about 90.9 Hz).
  Neither needs an assumed send rate. A counter wrap (uint32) is unwrapped, a large step back (the sender
  rebooted) starts the window over, and a repeated value counts once.
- ``drops`` is inferred from gaps in the receiver's device timestamp relative to the sender's send period.
  That period is either given (the simulator's rate, or ``--rate`` for a sender you know) or estimated from
  the median device-time interval, which is what a real port uses. It is only as good as that period, and it
  is what remains for packets without a counter (recordings).

A receiver reboot (device counter restarts) resets the drops baseline and is not counted as drops; a 32-bit
device counter wrap is unwrapped and counted normally.

Also per link: ``clip_fraction()``, the share of raw I/Q values at the int8 rails over the last
``clip_window_s`` seconds (high: the receiver's forced gain is too high for this sender), and the newest
packet's ``agc_gain``, ``fft_gain`` and ``noise_floor``.
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np

from .clock import RESET, WRAP, DeviceClock
from .parser import SEQ_MODULUS, CSIPacket, CSIParser, Stream, parse_stream

AUTO_INTERVALS = 200     # recent device-time intervals kept for the estimated send period
MIN_AUTO_INTERVALS = 50  # estimate (and drop counting) starts once this many intervals exist
SEQ_WINDOW_S = 10.0      # seq_loss and seq_rate cover this many recent host seconds
SEQ_MIN_PACKETS = 20     # seq_loss and seq_rate are None until the window holds this many packets
SEQ_REORDER = 64         # a counter step back of up to this many is a late or repeated packet; more is a reboot
CLIP_WINDOW_S = 5.0      # clip_fraction covers this many recent host seconds


@dataclass
class LinkHealth:
    """``send_rate_hz``: the sender's nominal rate, used only for ``drops``; None estimates it from device time."""

    send_rate_hz: Optional[float] = None
    window_s: float = 2.0
    drops: int = 0
    packets: int = 0
    reboots: int = 0          # receiver restarts seen (device counter went back); not drops
    wraps: int = 0            # 32-bit counter wraps seen (unwrapped; not drops)
    seq_window_s: float = SEQ_WINDOW_S
    clip_window_s: float = CLIP_WINDOW_S
    seq_restarts: int = 0     # sender restarts seen (its packet counter went back); the seq window starts over
    seq_repeats: int = 0      # packets whose counter value was already counted, or too late to place; ignored
    agc_gain: Optional[int] = None       # newest packet's receiver gains and noise floor (None: not printed)
    fft_gain: Optional[int] = None
    noise_floor: Optional[int] = None
    _recent: deque = field(default_factory=deque)            # host timestamps in window
    _latest_host: Optional[float] = None                      # newest host time, not arrival order
    _clock: DeviceClock = field(default_factory=DeviceClock)
    _last_t: Optional[int] = None                             # last in-order device time (us)
    _intervals: deque = field(default_factory=lambda: deque(maxlen=AUTO_INTERVALS))
    _seq: deque = field(default_factory=deque)                # (unwrapped counter, host_ts), counter order
    _seq_seen: set = field(default_factory=set)               # the unwrapped counters in _seq
    _seq_top: Optional[int] = None                            # highest unwrapped counter since the sender (re)started
    _clip: deque = field(default_factory=deque)               # (host_ts, clipped, n_raw) in the clip window
    _clipped: int = 0                                         # sums over _clip
    _raw: int = 0

    def observe(self, pkt: CSIPacket) -> None:
        """Account for one packet. Out-of-order/repeated timestamps add no drops."""
        self.packets += 1
        self._recent.append(pkt.host_ts)
        self._latest_host = pkt.host_ts if self._latest_host is None else max(self._latest_host, pkt.host_ts)
        horizon = self._latest_host - self.window_s
        while self._recent and self._recent[0] < horizon:
            self._recent.popleft()
        t, event = self._clock.advance(pkt.device_ts, pkt.host_ts)
        if event == RESET:
            self.reboots += 1
        elif event == WRAP:
            self.wraps += 1
        self._count_drops(t, rebooted=event == RESET)
        self._count_seq(pkt.seq, pkt.host_ts)
        self._observe_signal(pkt)

    def rate_hz(self) -> Optional[float]:
        """Send rate in Hz: the configured one, else the estimate (None until enough packets)."""
        if self.send_rate_hz is not None:
            return self.send_rate_hz
        if len(self._intervals) < MIN_AUTO_INTERVALS:
            return None
        return 1e6 / float(np.median(self._intervals))

    def _count_drops(self, t: int, rebooted: bool) -> None:
        last = self._last_t
        if rebooted or last is None:
            self._last_t = t  # new baseline: nothing to compare against yet
            return
        if t <= last:
            return  # repeated or out-of-order device time
        step = t - last
        self._last_t = t
        self._intervals.append(step)
        rate = self.rate_hz()
        if rate is not None:
            self.drops += max(0, round(step * rate / 1e6) - 1)

    def packet_rate(self) -> float:
        """Packets per second over the recent window (0 if no packets yet)."""
        return len(self._recent) / self.window_s

    def seq_loss(self) -> Optional[float]:
        """Fraction (0..1) of the sender's packets that never arrived, over the last ``seq_window_s`` seconds.

        Counted from gaps in the sender's packet counter, so it needs no assumed send rate: this is the loss
        to trust. None until the window holds SEQ_MIN_PACKETS packets, and for packets without a counter.
        """
        n = len(self._seq)
        if n < SEQ_MIN_PACKETS:
            return None
        expected = self._seq[-1][0] - self._seq[0][0] + 1
        return max(0.0, 1.0 - n / expected)

    def seq_rate(self) -> Optional[float]:
        """Packets per second the sender actually sends: its counter's span over the host-time span of the window.

        None until the window holds SEQ_MIN_PACKETS packets, and for packets without a counter.
        """
        if len(self._seq) < SEQ_MIN_PACKETS:
            return None
        (s0, t0), (s1, t1) = self._seq[0], self._seq[-1]
        return (s1 - s0) / (t1 - t0) if t1 > t0 else None

    def clip_fraction(self) -> Optional[float]:
        """Share (0..1) of raw I/Q values at the int8 rails over the last ``clip_window_s`` seconds.

        None until a packet with raw values arrives (packets not parsed from text have none).
        """
        return self._clipped / self._raw if self._raw else None

    def _count_seq(self, seq: Optional[int], host_ts: float) -> None:
        """Place one sender counter value in the window that seq_loss and seq_rate read."""
        if seq is None:
            return
        window, seen = self._seq, self._seq_seen
        horizon = self._latest_host - self.seq_window_s
        while window and window[0][1] < horizon:
            seen.discard(window.popleft()[0])
        s = seq
        if self._seq_top is not None:
            step = (seq - self._seq_top) % SEQ_MODULUS    # a uint32 wrap is a small step forward
            if step >= SEQ_MODULUS // 2:
                step -= SEQ_MODULUS                        # a step back
            if step >= -SEQ_REORDER:
                s = self._seq_top + step
            else:                                          # a big step back: the sender rebooted
                self.seq_restarts += 1
                window.clear()
                seen.clear()
                self._seq_top = None
        if s in seen or (window and s < window[0][0]):     # a repeat, or too late to place
            self.seq_repeats += 1
            return
        seen.add(s)
        if self._seq_top is None or s > self._seq_top:
            self._seq_top = s
            window.append((s, host_ts))
        else:
            bisect.insort(window, (s, host_ts))            # a late packet fills its gap

    def _observe_signal(self, pkt: CSIPacket) -> None:
        """The newest gains and noise floor, and the clipped share of raw values over ``clip_window_s``."""
        self.agc_gain, self.fft_gain, self.noise_floor = pkt.agc_gain, pkt.fft_gain, pkt.noise_floor
        clip, horizon = self._clip, self._latest_host - self.clip_window_s
        while clip and clip[0][0] < horizon:
            _, clipped, n_raw = clip.popleft()
            self._clipped -= clipped
            self._raw -= n_raw
        if pkt.n_raw:
            clip.append((pkt.host_ts, pkt.clipped, pkt.n_raw))
            self._clipped += pkt.clipped
            self._raw += pkt.n_raw

    def consume(self, stream: Stream, parser: Optional[CSIParser] = None) -> Iterator[CSIPacket]:
        """Parse ``stream``, observing every packet, and pass packets through."""
        for pkt in parse_stream(stream, parser):
            self.observe(pkt)
            yield pkt
