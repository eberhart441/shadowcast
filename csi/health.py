"""Link health from the packet stream: recent packet rate and estimated drops.

The parser never sees lost packets, so drops are inferred from gaps in the
device timestamp relative to the nominal send period.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .parser import CSIPacket, CSIParser, Stream, parse_stream


@dataclass
class LinkHealth:
    send_rate_hz: float = 100.0
    window_s: float = 2.0
    drops: int = 0
    packets: int = 0
    _recent: deque = field(default_factory=deque)   # host timestamps in window
    _last_device_ts: Optional[int] = None

    def observe(self, pkt: CSIPacket) -> None:
        """Account for one packet. Out-of-order/repeated timestamps add no drops."""
        self.packets += 1
        self._recent.append(pkt.host_ts)
        horizon = max(self._recent) - self.window_s
        while self._recent and self._recent[0] < horizon:
            self._recent.popleft()
        self._count_drops(pkt.device_ts)

    def _count_drops(self, device_ts: int) -> None:
        last = self._last_device_ts
        if last is None or device_ts > last:
            if last is not None:
                period_us = 1e6 / self.send_rate_hz
                self.drops += max(0, round((device_ts - last) / period_us) - 1)
            self._last_device_ts = device_ts

    def packet_rate(self) -> float:
        """Packets per second over the recent window (0 if no packets yet)."""
        return len(self._recent) / self.window_s

    def consume(self, stream: Stream, parser: Optional[CSIParser] = None) -> Iterator[CSIPacket]:
        """Parse ``stream``, observing every packet, and pass packets through."""
        for pkt in parse_stream(stream, parser):
            self.observe(pkt)
            yield pkt
