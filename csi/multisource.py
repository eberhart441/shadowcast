"""Read several receivers concurrently into one queue of (receiver_name, CSIPacket).

Each source is an iterable of (host_ts, line), the same seam as ``serial_stream``; a replayed source may
yield (host_ts, CSIPacket) instead. Each one is read on
its own thread with its own CSIParser, so one receiver's layout warm-up or malformed lines never touch
another's. A source that raises is recorded in ``errors()`` and the others keep running.

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
from typing import Mapping, Optional, Sequence

from .parser import CSIPacket, CSIParser, ParserStats, Stream

PENDING, RUNNING, DONE, STOPPED = "pending", "running", "done", "stopped"


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
    def __init__(self, sources: Mapping[str, Stream], close_timeout: float = 3.0) -> None:
        self._sources = dict(sources)
        self._queue: queue.Queue = queue.Queue()
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
                # a replayed recording yields parsed packets, which need no parsing
                packets = [line] if isinstance(line, CSIPacket) else self._parsers[name].feed(line, host_ts)
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
