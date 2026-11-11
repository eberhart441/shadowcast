"""Round recordings: every round broadcast a run's sources delivered, with labels, in one binary file.

    with RoundRecorder("run.rounds", room, ["gw1"], notes="bench") as rec:
        streams = rec.tee({"gw1": gateway_stream})          # or rec.write("gw1", host_ts, data) per broadcast
        rec.label("empty")                                  # host time now; or host_ts=... for a simulated run
        ...
    recording = load_rounds("run.rounds")                   # RecordingFormatError if the file cannot be used
    recording.streams(speed=8)                              # {source: (host time, round broadcast bytes)}, paced

A recording keeps the round broadcast bytes exactly as a source delivered them (undecoded, rejects included) with
their host time and source name, so a replay feeds the same input seam (docs/adr/0022-host-time-round-broadcast-
seam.md) the same bytes at the same host times, and the pipeline reaches the same link states and room state.
Labels mark from which host time on the room held what (``empty``, ``walking``...; ADR-0018 evaluates by them).
The header keeps the room config the run used, so the serials in the bytes can be named on replay.

The format, version 1, is defined in docs/round-recording.md. A reader refuses any other version, and any file
that is not a round recording, with a RecordingFormatError naming the file; a last record cut short (the recorder
was killed mid-write) is left out and counted in ``truncated_bytes``.
"""
from __future__ import annotations

import json
import math
import struct
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

from .recording import validate_label
from .room import Room, room_config, room_from_config
from .rounds import BroadcastStream

Pathlike = Union[str, Path]

MAGIC = b"SCROUNDS"
FORMAT_VERSION = 1
SUFFIX = ".rounds"
PREAMBLE = struct.Struct("<8sHI")      # magic, format version, header length (bytes of UTF-8 JSON that follow)
RECORD = struct.Struct("<BdBI")        # kind, host time (s), source index, body length; then the body
KIND_BROADCAST = 1                     # body: the round broadcast bytes as the source delivered them
KIND_LABEL = 2                         # body: a UTF-8 JSON object {"label": ..., any extra keys}
NO_SOURCE = 0xFF                       # the source index of a label
MAX_SOURCES = 255
FLUSH_EVERY = 500                      # records between flushes
FLUSH_S = 1.0                          # or wall seconds, whichever comes first
LABEL_KEYS_RESERVED = ("label",)


class RecordingFormatError(ValueError):
    """A file that is not a round recording this build can read; the message names the file and what is wrong."""


@dataclass(frozen=True)
class RecordedBroadcast:
    host_ts: float
    source: str
    data: bytes


@dataclass(frozen=True)
class Label:
    """From ``host_ts`` on (until the next label) the room held ``label``; ``extra`` carries any other keys."""
    host_ts: float
    label: str
    extra: Mapping = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------- writing

class RoundRecorder:
    """Writes one round recording. Thread-safe: each source's thread may write (``tee`` does)."""

    def __init__(self, path: Pathlike, room: Room, sources: Sequence[str], *, notes: str = "",
                 clock: Callable[[], float] = time.time) -> None:
        sources = tuple(sources)
        if len(set(sources)) != len(sources):
            raise ValueError(f"source names must be unique: {', '.join(sources)}")
        if len(sources) > MAX_SOURCES:
            raise ValueError(f"a round recording holds at most {MAX_SOURCES} sources, got {len(sources)}")
        self.path = Path(path)
        self._index = {name: k for k, name in enumerate(sources)}
        self._clock = clock
        self._lock = threading.Lock()
        self._pending = 0
        self._last_flush = time.monotonic()
        self.broadcasts = 0
        header = {"room": room_config(room), "sources": list(sources), "notes": notes, "started": clock()}
        body = json.dumps(header).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._out: Optional[BinaryIO] = open(self.path, "wb")
        self._out.write(PREAMBLE.pack(MAGIC, FORMAT_VERSION, len(body)) + body)
        self._out.flush()

    def write(self, source: str, host_ts: float, data: bytes) -> None:
        """One round broadcast as ``source`` delivered it at ``host_ts``."""
        index = self._index.get(source)
        if index is None:
            raise ValueError(f"source {source!r} is not one of this recording's sources: {', '.join(self._index)}")
        self._append(KIND_BROADCAST, host_ts, index, bytes(data))
        self.broadcasts += 1

    def label(self, label: str, host_ts: Optional[float] = None, **extra) -> None:
        """From ``host_ts`` on (default: the clock now, which is host time for live and real-time sources) the room
        holds ``label``. ``extra`` keys (for example a floor mark) must be JSON values."""
        label = validate_label(label)
        reserved = sorted(set(extra) & set(LABEL_KEYS_RESERVED))
        if reserved:
            raise ValueError(f"label keys reserved: {', '.join(reserved)}")
        ts = self._clock() if host_ts is None else float(host_ts)
        self._append(KIND_LABEL, ts, NO_SOURCE, json.dumps({"label": label, **extra}).encode("utf-8"))

    def tee(self, sources: Mapping[str, BroadcastStream]) -> dict[str, Iterator[tuple[float, Optional[bytes]]]]:
        """The same streams, each round broadcast also written here. Heartbeats pass through unwritten."""
        return {name: self._tee(name, stream) for name, stream in sources.items()}

    def _tee(self, name: str, stream: BroadcastStream) -> Iterator[tuple[float, Optional[bytes]]]:
        try:
            for host_ts, data in stream:
                if data is not None:
                    self.write(name, host_ts, data)
                yield host_ts, data
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
            self.flush()

    def _append(self, kind: int, host_ts: float, source: int, body: bytes) -> None:
        if not math.isfinite(host_ts):
            raise ValueError(f"host time must be finite, got {host_ts}")
        with self._lock:
            if self._out is None:
                raise ValueError(f"round recording {self.path} is closed")
            self._out.write(RECORD.pack(kind, host_ts, source, len(body)) + body)
            self._pending += 1
            now = time.monotonic()
            if self._pending >= FLUSH_EVERY or now - self._last_flush >= FLUSH_S:
                self._flush_locked(now)

    def flush(self) -> None:
        with self._lock:
            if self._out is not None:
                self._flush_locked(time.monotonic())

    def _flush_locked(self, now: float) -> None:
        assert self._out is not None
        self._out.flush()
        self._pending, self._last_flush = 0, now

    def close(self) -> None:
        with self._lock:
            if self._out is not None:
                self._out.close()
                self._out = None

    def __enter__(self) -> "RoundRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------------------------------- reading

@dataclass(frozen=True)
class RoundRecording:
    path: Path
    version: int
    header: Mapping
    room: Room
    sources: tuple[str, ...]
    broadcasts: tuple[RecordedBroadcast, ...]     # in file order: per source, the order it delivered them
    labels: tuple[Label, ...]                     # in host-time order
    truncated_bytes: int = 0                      # a last record cut short, left out

    @property
    def notes(self) -> str:
        return str(self.header.get("notes", ""))

    @property
    def started(self) -> Optional[float]:
        """Wall clock when recording started (seconds since the epoch)."""
        value = self.header.get("started")
        return float(value) if isinstance(value, (int, float)) else None

    def label_at(self, host_ts: float) -> Optional[Label]:
        """The label in force at ``host_ts``: the last one at or before it, None before the first."""
        k = bisect_right([lab.host_ts for lab in self.labels], host_ts)
        return self.labels[k - 1] if k else None

    def streams(self, speed: float = 1.0, *, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Iterator[tuple[float, bytes]]]:
        """Per source, its round broadcasts as ``(recorded host time, bytes)``, paced so that the gaps between
        recorded host times (across all sources) take ``1 / speed`` of their length on ``clock``;
        ``speed=math.inf`` does not wait. The host times themselves are yielded as recorded."""
        if not speed > 0:
            raise ValueError(f"replay speed must be positive, got {speed}")
        pacer = _Pacer(min((b.host_ts for b in self.broadcasts), default=0.0), speed, clock, sleep)
        by_source: dict[str, list[RecordedBroadcast]] = {name: [] for name in self.sources}
        for b in self.broadcasts:
            by_source[b.source].append(b)
        return {name: _paced(items, pacer) for name, items in by_source.items()}


class _Pacer:
    """Shared by every source of one replay: the first broadcast fixes the start, every other one waits its turn."""

    def __init__(self, first_ts: float, speed: float, clock: Callable[[], float],
                 sleep: Callable[[float], None]) -> None:
        self._first_ts, self._speed, self._clock, self._sleep = first_ts, speed, clock, sleep
        self._start: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self, host_ts: float) -> None:
        if self._speed == math.inf:
            return
        with self._lock:
            if self._start is None:
                self._start = self._clock()
            start = self._start
        delay = start + (host_ts - self._first_ts) / self._speed - self._clock()
        if delay > 0:
            self._sleep(delay)


def _paced(items: Iterable[RecordedBroadcast], pacer: _Pacer) -> Iterator[tuple[float, bytes]]:
    for b in items:
        pacer.wait(b.host_ts)
        yield b.host_ts, b.data


def load_rounds(path: Pathlike) -> RoundRecording:
    """Read a round recording. Raises RecordingFormatError if it is not one, or not a version this build reads."""
    path = Path(path)
    try:
        blob = path.read_bytes()
    except OSError as e:
        raise RecordingFormatError(f"cannot read round recording {path}: {e.strerror or e}") from e
    if len(blob) < PREAMBLE.size or not blob.startswith(MAGIC):
        raise RecordingFormatError(f"{path} is not a round recording (it does not start with {MAGIC.decode()})")
    _, version, header_len = PREAMBLE.unpack_from(blob)
    if version != FORMAT_VERSION:
        raise RecordingFormatError(f"{path} is a round recording of format version {version}; this build reads "
                                   f"version {FORMAT_VERSION} only")
    end = PREAMBLE.size + header_len
    if end > len(blob):
        raise RecordingFormatError(f"{path}: the header is cut short ({len(blob) - PREAMBLE.size} of {header_len} B)")
    header = _header(path, blob[PREAMBLE.size:end])
    sources = tuple(header["sources"])
    try:
        room = room_from_config(header["room"])
    except (KeyError, TypeError, ValueError) as e:
        raise RecordingFormatError(f"{path}: the header's room config is not usable ({e})") from e
    broadcasts, labels = [], []
    offset = end
    while offset < len(blob):
        if offset + RECORD.size > len(blob):
            break
        kind, host_ts, source, length = RECORD.unpack_from(blob, offset)
        body_at = offset + RECORD.size
        if body_at + length > len(blob):
            break
        body = blob[body_at:body_at + length]
        if kind == KIND_BROADCAST:
            if source >= len(sources):
                raise RecordingFormatError(f"{path}: record at byte {offset} names source {source}, but the header "
                                           f"lists {len(sources)}")
            broadcasts.append(RecordedBroadcast(host_ts, sources[source], body))
        elif kind == KIND_LABEL:
            labels.append(_label(path, offset, host_ts, body))
        else:
            raise RecordingFormatError(f"{path}: record at byte {offset} has kind {kind}, which format version "
                                       f"{FORMAT_VERSION} does not define")
        offset = body_at + length
    labels.sort(key=lambda lab: lab.host_ts)
    return RoundRecording(path, version, header, room, sources, tuple(broadcasts), tuple(labels),
                          truncated_bytes=len(blob) - offset)


def _header(path: Path, raw: bytes) -> dict:
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RecordingFormatError(f"{path}: the header is not UTF-8 JSON ({e})") from e
    if not isinstance(header, dict) or not isinstance(header.get("sources"), list) \
            or not all(isinstance(s, str) for s in header["sources"]) or not isinstance(header.get("room"), dict):
        raise RecordingFormatError(f"{path}: the header needs a room config and a list of source names")
    return header


def _label(path: Path, offset: int, host_ts: float, body: bytes) -> Label:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RecordingFormatError(f"{path}: label at byte {offset} is not UTF-8 JSON ({e})") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("label"), str):
        raise RecordingFormatError(f"{path}: label at byte {offset} has no \"label\" string")
    label = obj.pop("label")
    return Label(host_ts, label, obj)
