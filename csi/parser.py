"""Parse esp-csi ``CSI_DATA`` serial lines into per-packet amplitude vectors.

Line format (esp-csi ``csi_recv``)::

    CSI_DATA,<id>,<mac>,<rssi>,...,<channel>,...,<local_timestamp>,...,<len>,<first_word>,"[i0,r0,i1,r1,...]"

The firmware prints a header line (``type,id,mac,rssi,...,data``) at boot. We
use it to map column names to indices; if it was missed (e.g. we opened the port
after boot) we fall back to the standard esp-csi column order.

Values are int8 pairs, imaginary first then real, per subcarrier. Amplitude is
``hypot(imag, real)`` so the order does not matter for amplitude.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

import numpy as np

DEFAULT_HEADER = (
    "type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,"
    "aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,"
    "secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data"
)
# Header printed by esp-csi csi_recv on ESP32-C5 (verified on hardware, IDF v5.5.6).
C5_HEADER = (
    "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,"
    "local_timestamp,sig_len,rx_format,len,first_word,data"
)
MAX_LINE_BYTES = 16384
KNOWN_HEADERS = (DEFAULT_HEADER, C5_HEADER)
# Subcarriers that are always null for a given raw int count, measured on hardware.
# Deriving them from one packet is unreliable: at low CSI gain most values are 0/+-1,
# so a single packet's zeros are not a stable guard-band mask.
KNOWN_NULL_SUBCARRIERS = {234: (57, 58, 59)}  # ESP32-C5, 117 subcarriers
# Unknown raw lengths: the first WARMUP_PACKETS packets of that length are held, then the mask
# keeps every subcarrier that is non-zero in at least one of them. The layout re-locks when most
# of the last RELOCK_WINDOW packets share one new raw length.
WARMUP_PACKETS = 20
RELOCK_WINDOW = 50
RELOCK_FRACTION = 0.8
_ALIASES = {"primary_channel": "channel", "timestamp": "local_timestamp"}
_REQUIRED = ("rssi", "channel", "local_timestamp", "data")

# A stream is an iterable of (host_timestamp_seconds, text_line).
Stream = Iterable[tuple[float, str]]


@dataclass(frozen=True)
class CSIPacket:
    host_ts: float          # laptop clock, seconds (time.time() or synthetic)
    device_ts: int          # ESP32 local_timestamp, microseconds (raw 32-bit counter)
    rssi: int
    channel: int
    amplitude: np.ndarray   # float32, one value per kept (non-null) subcarrier
    mac: str = ""           # sender MAC, lowercase; "" when the header has no mac column


@dataclass
class ParserStats:
    parsed: int = 0
    malformed: int = 0       # CSI_DATA lines we could not parse
    layout_skipped: int = 0  # valid lines whose raw length is not the locked/candidate one
    ignored: int = 0         # non-CSI lines (boot logs, header, blanks)
    lines: int = 0
    relocks: int = 0         # times the layout was re-locked to a new raw length


def parse_header(line: str) -> Optional[dict[str, int]]:
    """Return {column_name: index} if ``line`` is the firmware header, else None."""
    fields = [f.strip().strip('"') for f in line.strip().split(",")]
    fields = [_ALIASES.get(f, f) for f in fields]
    if fields and fields[0] == "type" and all(r in fields for r in _REQUIRED):
        return {name: i for i, name in enumerate(fields)}
    return None


@dataclass
class _Draft:
    """A parsed packet before the null mask is applied."""

    host_ts: float
    device_ts: int
    rssi: int
    channel: int
    raw_len: int            # raw int count (I and Q values), the layout key
    amp: np.ndarray         # every raw subcarrier, float32
    mac: str = ""


@dataclass
class CSIParser:
    """Stateful line parser. Never raises on bad input; counts instead.

    ``parse_line`` returns the packet for a line only when it can be emitted at once.
    Packets of an unknown layout are held during warm-up and released in one batch,
    in order, by the line that locks the layout. ``feed`` returns every packet that is
    ready, so use it (or ``parse_stream``) to see the whole stream.
    """

    columns: dict[str, int] = field(
        default_factory=lambda: parse_header(DEFAULT_HEADER) or {}
    )
    stats: ParserStats = field(default_factory=ParserStats)
    _layout_known: bool = False             # True once a header line was seen or inferred
    _raw_len: Optional[int] = None          # raw int count of the locked layout
    _keep: Optional[np.ndarray] = None      # bool mask over subcarriers (None until locked)
    _candidate: Optional[int] = None        # raw length under warm-up
    _held: list[_Draft] = field(default_factory=list)
    _ready: list[CSIPacket] = field(default_factory=list)
    _recent_lens: deque = field(default_factory=lambda: deque(maxlen=RELOCK_WINDOW))

    @property
    def kept_subcarriers(self) -> Optional[list[int]]:
        """Raw subcarrier indices kept after null removal (None before the layout is locked)."""
        return None if self._keep is None else np.flatnonzero(self._keep).tolist()

    def parse_line(self, line: str, host_ts: Optional[float] = None) -> Optional[CSIPacket]:
        """Parse one line. Returns a packet emitted now, or None (header/ignored/bad/held)."""
        self.stats.lines += 1
        line = line.strip()
        if not line.startswith("CSI_DATA"):
            header = parse_header(line)
            if header is not None:
                self.columns, self._layout_known = header, True
            self.stats.ignored += 1
            return None
        try:
            draft = self._parse_csi(line, time.time() if host_ts is None else host_ts)
            return self._admit(draft)
        except (ValueError, IndexError, KeyError):
            self.stats.malformed += 1
            return None

    def feed(self, line: str, host_ts: Optional[float] = None) -> list[CSIPacket]:
        """Parse one line and return every packet now ready, in arrival order."""
        pkt = self.parse_line(line, host_ts)
        ready, self._ready = self._ready, []
        return ready + ([pkt] if pkt is not None else [])

    def _parse_csi(self, line: str, host_ts: float) -> _Draft:
        lb, rb = line.find("["), line.rfind("]")
        if lb < 0 or rb < lb:
            raise ValueError("missing or partial data list")
        prefix = line[:lb].rstrip('", ')
        fields = [f.strip() for f in prefix.split(",")]
        if not self._layout_known:
            self._infer_layout(len(fields))
        if len(fields) != self.columns["data"]:
            raise ValueError("column count does not match header")
        raw = np.array(line[lb + 1:rb].replace(",", " ").split(), dtype=np.int64)
        if raw.size == 0 or raw.size % 2:
            raise ValueError("data must be non-empty I/Q pairs")
        if raw.min() < -128 or raw.max() > 127:
            raise ValueError("value outside int8 range")
        declared = self.columns.get("len")
        if declared is not None and int(fields[declared]) != raw.size:
            raise ValueError("len column disagrees with data length")
        c = self.columns
        iq = raw.reshape(-1, 2).astype(np.float32)
        return _Draft(
            host_ts=host_ts,
            device_ts=int(fields[c["local_timestamp"]]),
            rssi=int(fields[c["rssi"]]),
            channel=int(fields[c["channel"]]),
            raw_len=raw.size,
            amp=np.hypot(iq[:, 0], iq[:, 1]),
            mac=fields[c["mac"]].strip('"').lower() if "mac" in c else "",
        )

    def _infer_layout(self, n_fields: int) -> None:
        """No header seen (port opened after boot): match a known layout by column count."""
        for text in KNOWN_HEADERS:
            cols = parse_header(text) or {}
            if cols.get("data") == n_fields:
                self.columns = cols
                break
        self._layout_known = True

    def _admit(self, d: _Draft) -> Optional[CSIPacket]:
        """Layout state machine for one parsed packet: emit, hold for warm-up, or skip."""
        n = d.raw_len
        if self._keep is not None:
            self._recent_lens.append(n)
            if n == self._raw_len:
                return self._emit(d)
            self.stats.layout_skipped += 1
            self._maybe_relock()
            return None
        if n in KNOWN_NULL_SUBCARRIERS:
            self._discard_held()
            self._lock(n, _known_keep(n))
            return self._emit(d)
        if self._candidate is None:
            self._candidate = n
        if n != self._candidate:
            self.stats.layout_skipped += 1
            return None
        self._held.append(d)
        if len(self._held) >= WARMUP_PACKETS:
            self._finish_warm_up(n)
        return None

    def _finish_warm_up(self, raw_len: int) -> None:
        keep = np.zeros(self._held[0].amp.size, dtype=bool)
        for h in self._held:
            keep |= h.amp > 0
        if not keep.any():  # zero in every warm-up packet: nothing usable, start over
            self.stats.malformed += len(self._held)
            self._held.clear()
            self._candidate = None
            return
        self._lock(raw_len, keep)
        self._ready.extend(self._emit(h) for h in self._held)
        self._held.clear()

    def _lock(self, n: int, keep: np.ndarray) -> None:
        self._raw_len, self._keep, self._candidate = n, keep, None
        self._recent_lens.clear()

    def _discard_held(self) -> None:
        self.stats.layout_skipped += len(self._held)
        self._held.clear()
        self._candidate = None

    def _maybe_relock(self) -> None:
        if len(self._recent_lens) < RELOCK_WINDOW:
            return
        others = Counter(x for x in self._recent_lens if x != self._raw_len)
        if not others:
            return
        new_len, count = others.most_common(1)[0]
        if count > RELOCK_FRACTION * RELOCK_WINDOW:
            self.stats.relocks += 1
            self._raw_len, self._keep = None, None
            self._candidate = new_len
            self._recent_lens.clear()

    def _emit(self, d: _Draft) -> CSIPacket:
        self.stats.parsed += 1
        return CSIPacket(host_ts=d.host_ts, device_ts=d.device_ts, rssi=d.rssi,
                         channel=d.channel, amplitude=d.amp[self._keep], mac=d.mac)


def _known_keep(n: int) -> np.ndarray:
    """Mask for a raw length with measured nulls (see KNOWN_NULL_SUBCARRIERS)."""
    keep = np.ones(n // 2, dtype=bool)
    keep[list(KNOWN_NULL_SUBCARRIERS[n])] = False
    return keep


def parse_stream(stream: Stream, parser: Optional[CSIParser] = None) -> Iterator[CSIPacket]:
    """Turn a (host_ts, line) stream into packets, sharing ``parser`` stats. Heartbeats (line None) are skipped."""
    parser = parser or CSIParser()
    for host_ts, line in stream:
        if line is not None:
            yield from parser.feed(line, host_ts)


def serial_stream(port: str, baud: int = 921600, timeout: float = 1.0) -> Iterator[tuple[float, Optional[str]]]:
    """Yield (time.time(), line) from a serial port until closed/interrupted.

    The port is read in chunks (everything waiting, or 1 byte with ``timeout``), not a byte at a time, and the
    complete lines of a chunk share the time the chunk arrived. Lines come without their line ending.
    A read that times out with no data yields (time.time(), None), a heartbeat: a consumer can then check its
    stop flag at least every ``timeout`` seconds, and closing the generator closes the port. parse_stream and
    MultiSource skip heartbeats, so they see only lines.

    DTR and RTS drive reset and boot mode on the ESP32 boards' auto-reset circuit: DTR released while RTS is
    asserted holds the chip in reset, and a receiver that resets re-rolls its forced gain. The OS asserts both
    lines when a port opens, so the port opens with pyserial's defaults (both asserted) and then releases RTS
    before DTR: both, then DTR only, then neither, never the reset state. Releasing them in the other order, or
    setting both off before the open (pyserial then applies DTR first), passes through it. This should keep
    opening a port (e.g. restarting the dashboard) from resetting the board; it is not yet confirmed on the rig.
    """
    import serial  # imported lazily so --simulate works without a port

    ser = serial.Serial()  # unopened, so the settings below are in place when it opens
    ser.port, ser.baudrate, ser.timeout = port, baud, timeout
    ser.open()                 # pyserial's defaults: DTR and RTS asserted, as the OS leaves them on open
    try:
        ser.rts = False        # RTS first: DTR asserted with RTS released does not reset the board
        ser.dtr = False
        ser.reset_input_buffer()
        pending = b""
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            now = time.time()
            if not chunk:
                yield now, None
                continue
            *lines, pending = (pending + chunk).split(b"\n")
            if len(pending) > MAX_LINE_BYTES:  # no newline in far too long: not CSI, hand it on
                lines.append(pending)
                pending = b""
            for raw in lines:
                yield now, raw.rstrip(b"\r").decode("utf-8", errors="replace")
    finally:
        ser.close()
