"""Monotonic device time from the ESP32 ``local_timestamp`` counter.

``local_timestamp`` is a 32-bit microsecond counter. It wraps to 0 about every
71.6 minutes, and it restarts from 0 when the receiver reboots. Subtracting raw
values across either event gives a negative gap, which stalls anything that
prunes or counts by device time. ``DeviceClock`` turns the raw counter into a
timeline that keeps moving forward, and says which kind of jump it saw.
"""
from __future__ import annotations

from typing import Optional

WRAP_US = 2 ** 32
REORDER_US = 1_000_000     # a backward step this small is an out-of-order packet, not a counter event
WRAP_TOLERANCE_S = 0.5     # host-clock agreement needed to read a backward step as a wrap

NORMAL = ""
WRAP = "wrap"              # counter ran past 2**32 and continued near 0; timeline continues
RESET = "reset"            # receiver rebooted; timeline restarts at the new raw value


class DeviceClock:
    """Feed raw ``device_ts`` and ``host_ts`` in arrival order; get a monotonic timeline back.

    ``advance`` returns ``(t_us, event)``. ``t_us`` is raw plus the accumulated wrap offset.
    After a ``RESET`` the timeline starts over, so consumers must drop state built on the
    old timeline. A wrap needs no such handling. Out-of-order packets (a small backward step)
    return ``NORMAL`` and do not move the baseline.

    A backward step is a wrap only when the host clock agrees: the wrapped step
    (``raw + 2**32 - last``) must match the elapsed host time. A reboot's backward step
    is almost always far larger than the host gap, so it is classified as a reset. If no
    usable host gap exists, only a counter that was in the top half and came back to the
    bottom half is taken as a wrap.
    """

    def __init__(self) -> None:
        self._raw: Optional[int] = None
        self._host: float = 0.0
        self._offset = 0

    def advance(self, raw: int, host_ts: float) -> tuple[int, str]:
        if self._raw is None:
            self._raw, self._host = raw, host_ts
            return raw, NORMAL
        delta = raw - self._raw
        if delta >= -REORDER_US:
            if delta >= 0:
                self._raw, self._host = raw, host_ts
            return raw + self._offset, NORMAL
        event = WRAP if self._is_wrap(raw, host_ts) else RESET
        self._offset = self._offset + WRAP_US if event == WRAP else 0
        self._raw, self._host = raw, host_ts
        return raw + self._offset, event

    def _is_wrap(self, raw: int, host_ts: float) -> bool:
        gap_s = host_ts - self._host
        if gap_s > 0:
            wrapped_step_s = (raw + WRAP_US - self._raw) / 1e6
            return abs(wrapped_step_s - gap_s) <= max(WRAP_TOLERANCE_S, 0.5 * gap_s)
        return self._raw >= WRAP_US // 2 > raw
