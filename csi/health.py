"""Link health from the packet stream: recent packet rate and estimated drops.

The parser never sees lost packets, so drops are inferred from gaps in the
device timestamp relative to the sender's send period. That period is either
given (the simulator's rate, or ``--rate`` for a sender you know) or estimated
from the median device-time interval, which is what a real port uses.

A receiver reboot (device counter restarts) resets the baseline and is not
counted as drops; a 32-bit counter wrap is unwrapped and counted normally.

A transceiver link has no packets on the host, only the receiving node's link reports: ``ReportHealth`` takes
its rate from how often those arrive, scaled by the share of recent rounds the transmitter was heard in.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np

from .broadcast import HEARD_WINDOW_ROUNDS, LinkReport
from .clock import RESET, WRAP, DeviceClock
from .parser import CSIPacket, CSIParser, Stream, parse_stream

NO_SIGNAL_S = 2.0        # a link with no packet or link report for longer is silent (NO_SIGNAL, not warm)
AUTO_INTERVALS = 200     # recent device-time intervals kept for the estimated send period
MIN_AUTO_INTERVALS = 50  # estimate (and drop counting) starts once this many intervals exist


@dataclass
class LinkHealth:
    """``send_rate_hz``: the sender's nominal rate. None estimates it from device timestamps."""

    send_rate_hz: Optional[float] = None
    window_s: float = 2.0
    drops: int = 0
    packets: int = 0
    reboots: int = 0          # receiver restarts seen (device counter went back); not drops
    wraps: int = 0            # 32-bit counter wraps seen (unwrapped; not drops)
    _recent: deque = field(default_factory=deque)            # host timestamps in window
    _latest_host: Optional[float] = None                      # newest host time, not arrival order
    _clock: DeviceClock = field(default_factory=DeviceClock)
    _last_t: Optional[int] = None                             # last in-order device time (us)
    _intervals: deque = field(default_factory=lambda: deque(maxlen=AUTO_INTERVALS))

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

    def consume(self, stream: Stream, parser: Optional[CSIParser] = None) -> Iterator[CSIPacket]:
        """Parse ``stream``, observing every packet, and pass packets through."""
        for pkt in parse_stream(stream, parser):
            self.observe(pkt)
            yield pkt


class ReportHealth:
    """Health of a transceiver link from its link reports.

    ``packets`` counts the link reports received. ``drops`` counts the rounds in which the receiving node reported
    not hearing the transmitter (a report whose ``since`` is not 0). ``packet_rate`` is how often reports arrive
    over the last ``window_s`` of host time (the receiving node's round rate, as the host hears it) times the share
    of recent rounds in which the transmitter was heard (``heard`` / 15).
    """

    def __init__(self, window_s: float = 2.0) -> None:
        self.window_s = window_s
        self.packets = 0
        self.drops = 0
        self._recent: deque = deque()
        self._latest_host: Optional[float] = None
        self._heard = 0

    def observe_report(self, host_ts: float, report: LinkReport) -> None:
        self.packets += 1
        self._heard = report.heard
        if report.since:
            self.drops += 1
        self._recent.append(host_ts)
        self._latest_host = host_ts if self._latest_host is None else max(self._latest_host, host_ts)
        while self._recent and self._recent[0] < self._latest_host - self.window_s:
            self._recent.popleft()

    def packet_rate(self) -> float:
        return len(self._recent) / self.window_s * self._heard / HEARD_WINDOW_ROUNDS
