"""Raw CSI recording: every line a receiver delivers, with its host time, for exact offline replays.

``tee_sources`` wraps the dashboard's named (host_ts, line) streams so each line is also appended to a file
as ``host_ts<TAB>receiver<TAB>line``. ``read_raw`` reads such a file back as per-receiver streams in the
same (host_ts, line) form, so a recorded run can be fed through the same pipeline again (scripts/accuracy_eval.py).
The file is append-only and line-buffered in batches; a run of four links is about 130 KB/s.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Union

FLUSH_EVERY = 200   # lines between flushes


def tee_sources(sources: Mapping[str, Iterable[tuple[float, Optional[str]]]],
                path: Union[str, Path]) -> dict[str, Iterator[tuple[float, Optional[str]]]]:
    """The same streams, each line also appended to ``path``. Heartbeats (line None) are not written."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out = open(path, "a", encoding="utf-8")
    lock = threading.Lock()
    pending = [0]

    def tee(name: str, stream: Iterable[tuple[float, Optional[str]]]) -> Iterator[tuple[float, Optional[str]]]:
        try:
            for host_ts, line in stream:
                if line is not None:
                    text = line.rstrip("\r\n")
                    with lock:
                        out.write(f"{host_ts:.6f}\t{name}\t{text}\n")
                        pending[0] += 1
                        if pending[0] >= FLUSH_EVERY:
                            out.flush()
                            pending[0] = 0
                yield host_ts, line
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
            with lock:
                out.flush()

    return {name: tee(name, stream) for name, stream in sources.items()}


def read_raw(path: Union[str, Path], t_from: float = float("-inf"),
             t_to: float = float("inf")) -> dict[str, list[tuple[float, str]]]:
    """Per receiver, the recorded (host_ts, line) pairs in file order, optionally only within [t_from, t_to)."""
    streams: dict[str, list[tuple[float, str]]] = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as f:
        for row in f:
            parts = row.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                ts = float(parts[0])
            except ValueError:
                continue
            if t_from <= ts < t_to:
                streams[parts[1]].append((ts, parts[2]))
    return dict(streams)
