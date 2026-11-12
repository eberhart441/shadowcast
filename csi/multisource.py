"""Read several receivers concurrently into one queue of (receiver_name, CSIPacket).

Each source is an iterable of (host_ts, line), the same seam as ``serial_stream``; a replayed source may
yield (host_ts, CSIPacket) instead, and a transceiver source yields (host_ts, round broadcast bytes), which are
queued undecoded as a ``HeardBroadcast`` (csi/rounds.py). Each one is read on
its own thread with its own CSIParser, so one receiver's layout warm-up or malformed lines never touch
another's. A source that raises is recorded in ``errors()`` and the others keep running.

Round broadcasts waiting in the queue are bounded (``max_queued_broadcasts``), so a consumer that falls behind
cannot grow memory without limit. A live round broadcast (stamped within ``WALL_CLOCK_TOLERANCE_S`` of the wall
clock: a gateway, the realtime simulator) cannot wait, so one that finds the queue full is dropped and counted
per source (``take_dropped()``); a recorded or fast-simulated one waits for room instead, which keeps replays
complete and deterministic.

    ms = MultiSource({"rx1": serial_stream("/dev/cu.usbmodem2101"), "rx2": serial_stream("/dev/cu.usbserial-0001")})
    ms.start()
    for name, pkt in ms.drain(timeout=0.5): ...
    ms.close()        # stops every reader and closes every port

``parse_port_specs`` turns ``--port`` values into (name, path) pairs: ``rx1=/dev/...`` is named, a bare
path gets the next free ``rxN``.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Mapping, Optional, Sequence

from .parser import CSIPacket, CSIParser, ParserStats, Stream
from .rounds import HeardBroadcast

PENDING, RUNNING, DONE, STOPPED = "pending", "running", "done", "stopped"
MAX_QUEUED_BROADCASTS = 4096   # about 7 s of a 30-node rig at 20 Hz (600 round broadcasts/s), ~2 MB
WALL_CLOCK_TOLERANCE_S = 5.0   # a host time this close to the wall clock is live, not a recording
WAIT_TICK_S = 0.1              # how often a waiting reader re-checks for close()


def parse_port_specs(values: Sequence[str]) -> list[tuple[str, str]]:
    """``["rx1=/dev/a", "/dev/b"]`` -> ``[("rx1", "/dev/a"), ("rx2", "/dev/b")]``.

    A value is named when the part before the first ``=`` has no ``/`` (device paths never do).
    Unnamed ports get ``rx1``, ``rx2``, ... skipping any name already given explicitly.
    """
    named: list[tuple[Optional[str], str]] = []
    for v in values:
        left, sep, right = v.partition("=")
        if sep and "/" not in left and left:
            named.append((left, right))
        else:
            named.append((None, v))
    taken = {name for name, _ in named if name}
    out, n = [], 0
    for name, path in named:
        if name is None:
            n += 1
            while f"rx{n}" in taken:
                n += 1
            name = f"rx{n}"
            taken.add(name)
        if not path:
            raise ValueError(f"port {name!r} has no path")
        out.append((name, path))
    seen: set[str] = set()
    for name, _ in out:
        if name in seen:
            raise ValueError(f"duplicate receiver name {name!r}")
        seen.add(name)
    return out


class MultiSource:
    def __init__(self, sources: Mapping[str, Stream], close_timeout: float = 3.0,
                 max_queued_broadcasts: Optional[int] = None) -> None:
        self._sources = dict(sources)
        self._queue: queue.Queue = queue.Queue()
        self._max_broadcasts = MAX_QUEUED_BROADCASTS if max_queued_broadcasts is None else max_queued_broadcasts
        self._space = threading.Condition()    # guards _queued and _dropped
        self._queued = 0                        # round broadcasts in the queue now
        self._dropped: dict[str, int] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, str] = {name: PENDING for name in self._sources}
        self._errors: dict[str, str] = {}
        self._parsers = {name: CSIParser() for name in self._sources}
        self._threads: dict[str, threading.Thread] = {}
        self._close_timeout = close_timeout

    def start(self) -> None:
        for name in self._sources:
            t = threading.Thread(target=self._read, args=(name,), name=f"csi-read-{name}", daemon=True)
            self._threads[name] = t
            t.start()

    def _read(self, name: str) -> None:
        stream = self._sources[name]
        self._set(name, RUNNING)
        try:
            for host_ts, line in stream:
                if self._stop.is_set():
                    break
                if line is None:  # heartbeat from a quiet port: only the stop check above matters
                    continue
                # a replayed recording yields parsed packets, which need no parsing; round broadcasts are
                # decoded downstream, where rejections are counted
                if isinstance(line, (bytes, bytearray)):
                    if self._make_room(name, host_ts):
                        self._queue.put((name, HeardBroadcast(host_ts, bytes(line))))
                    continue
                if isinstance(line, CSIPacket):
                    packets = [line]
                else:
                    packets = self._parsers[name].feed(line, host_ts)
                for pkt in packets:
                    self._queue.put((name, pkt))
            self._set(name, STOPPED if self._stop.is_set() else DONE)
        except Exception as e:  # a failing port must not take the others down
            with self._lock:
                self._errors[name] = str(e) or type(e).__name__
                self._status[name] = f"error: {self._errors[name]}"
        finally:
            close = getattr(stream, "close", None)  # releases the port on this thread
            if close:
                try:
                    close()
                except Exception:
                    pass

    def _make_room(self, name: str, host_ts: float) -> bool:
        """Take a place in the queue for one round broadcast. A live one that finds none is dropped and counted;
        a recorded one waits (False only when the reader is being closed)."""
        live = abs(time.time() - host_ts) <= WALL_CLOCK_TOLERANCE_S
        with self._space:
            while self._queued >= self._max_broadcasts:
                if live:
                    self._dropped[name] = self._dropped.get(name, 0) + 1
                    return False
                if self._stop.is_set():
                    return False
                self._space.wait(WAIT_TICK_S)
            self._queued += 1
            return True

    def take_dropped(self) -> dict[str, int]:
        """Round broadcasts dropped on a full queue since the last call, by source."""
        with self._space:
            dropped, self._dropped = self._dropped, {}
        return dropped

    def queued_broadcasts(self) -> int:
        """Round broadcasts waiting in the queue now (at most ``max_queued_broadcasts``)."""
        with self._space:
            return self._queued

    def _set(self, name: str, status: str) -> None:
        with self._lock:
            if not self._status[name].startswith("error"):
                self._status[name] = status

    def drain(self, timeout: float = 0.0) -> list[tuple[str, CSIPacket]]:
        """Packets available now. Waits up to ``timeout`` seconds for the first one."""
        out: list[tuple[str, CSIPacket]] = []
        try:
            out.append(self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait())
        except queue.Empty:
            return out
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        taken = sum(isinstance(pkt, HeardBroadcast) for _, pkt in out)
        if taken:
            with self._space:
                self._queued -= taken
                self._space.notify_all()
        return out

    def status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._status)

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def mark_failed(self, name: str, message: str) -> None:
        """Record a receiver as failed from outside its reader (its packets were rejected downstream)."""
        with self._lock:
            self._errors[name] = message
            self._status[name] = f"error: {message}"

    def parser_stats(self, name: str) -> ParserStats:
        return self._parsers[name].stats

    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads.values())

    def finished(self) -> bool:
        """Every reader has ended and every packet has been drained."""
        return not self.alive() and self._queue.empty()

    def close(self) -> None:
        """Stop all readers; each closes its own port before its thread ends."""
        self._stop.set()
        for t in self._threads.values():
            t.join(timeout=self._close_timeout)

    def __enter__(self) -> "MultiSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
