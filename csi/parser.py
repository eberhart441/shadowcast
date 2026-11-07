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
KNOWN_HEADERS = (DEFAULT_HEADER, C5_HEADER)
_ALIASES = {"primary_channel": "channel", "timestamp": "local_timestamp"}
_REQUIRED = ("rssi", "channel", "local_timestamp", "data")

# A stream is an iterable of (host_timestamp_seconds, text_line).
Stream = Iterable[tuple[float, str]]


@dataclass(frozen=True)
class CSIPacket:
    host_ts: float          # laptop clock, seconds (time.time() or synthetic)
    device_ts: int          # ESP32 local_timestamp, microseconds
    rssi: int
    channel: int
    amplitude: np.ndarray   # float32, one value per kept (non-null) subcarrier


@dataclass
class ParserStats:
    parsed: int = 0
    malformed: int = 0      # CSI_DATA lines we could not parse
    layout_skipped: int = 0  # valid lines with a different raw length than locked
    ignored: int = 0        # non-CSI lines (boot logs, header, blanks)
    lines: int = 0


def parse_header(line: str) -> Optional[dict[str, int]]:
    """Return {column_name: index} if ``line`` is the firmware header, else None."""
    fields = [f.strip().strip('"') for f in line.strip().split(",")]
    fields = [_ALIASES.get(f, f) for f in fields]
    if fields and fields[0] == "type" and all(r in fields for r in _REQUIRED):
        return {name: i for i, name in enumerate(fields)}
    return None


@dataclass
class CSIParser:
    """Stateful line parser. Never raises on bad input; counts instead."""

    columns: dict[str, int] = field(
        default_factory=lambda: parse_header(DEFAULT_HEADER) or {}
    )
    stats: ParserStats = field(default_factory=ParserStats)
    _layout_known: bool = False             # True once a header line was seen or inferred
    _raw_len: Optional[int] = None          # raw int count locked from first packet
    _keep: Optional[np.ndarray] = None      # bool mask over subcarriers

    @property
    def kept_subcarriers(self) -> Optional[list[int]]:
        """Raw subcarrier indices kept after null removal (None before first packet)."""
        return None if self._keep is None else np.flatnonzero(self._keep).tolist()

    def parse_line(self, line: str, host_ts: Optional[float] = None) -> Optional[CSIPacket]:
        """Parse one line. Returns a packet, or None (header/ignored/bad)."""
        self.stats.lines += 1
        line = line.strip()
        if not line.startswith("CSI_DATA"):
            header = parse_header(line)
            if header is not None:
                self.columns, self._layout_known = header, True
            self.stats.ignored += 1
            return None
        try:
            pkt = self._parse_csi(line, time.time() if host_ts is None else host_ts)
        except (ValueError, IndexError, KeyError):
            self.stats.malformed += 1
            return None
        if pkt is None:
            self.stats.layout_skipped += 1
            return None
        self.stats.parsed += 1
        return pkt

    def _parse_csi(self, line: str, host_ts: float) -> Optional[CSIPacket]:
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
        amp = self._amplitude(raw)
        if amp is None:
            return None
        c = self.columns
        return CSIPacket(
            host_ts=host_ts,
            device_ts=int(fields[c["local_timestamp"]]),
            rssi=int(fields[c["rssi"]]),
            channel=int(fields[c["channel"]]),
            amplitude=amp,
        )

    def _infer_layout(self, n_fields: int) -> None:
        """No header seen (port opened after boot): match a known layout by column count."""
        for text in KNOWN_HEADERS:
            cols = parse_header(text) or {}
            if cols.get("data") == n_fields:
                self.columns = cols
                break
        self._layout_known = True

    def _amplitude(self, raw: np.ndarray) -> Optional[np.ndarray]:
        """Amplitude per subcarrier with null (all-zero) subcarriers dropped.

        The null mask is locked from the first packet so every output has the
        same length; packets with a different raw length are skipped.
        """
        iq = raw.reshape(-1, 2).astype(np.float32)
        amp = np.hypot(iq[:, 0], iq[:, 1])
        if self._raw_len is None:
            keep = amp > 0
            if not keep.any():
                raise ValueError("all subcarriers zero")
            self._raw_len, self._keep = raw.size, keep
        elif raw.size != self._raw_len:
            return None
        return amp[self._keep]


def parse_stream(stream: Stream, parser: Optional[CSIParser] = None) -> Iterator[CSIPacket]:
    """Turn a (host_ts, line) stream into packets, sharing ``parser`` stats."""
    parser = parser or CSIParser()
    for host_ts, line in stream:
        pkt = parser.parse_line(line, host_ts)
        if pkt is not None:
            yield pkt


def serial_stream(port: str, baud: int = 921600, timeout: float = 1.0) -> Iterator[tuple[float, str]]:
    """Yield (time.time(), line) from a serial port until closed/interrupted."""
    import serial  # imported lazily so --simulate works without a port

    with serial.Serial(port, baud, timeout=timeout) as ser:
        ser.reset_input_buffer()
        while True:
            raw = ser.readline()
            if raw:
                yield time.time(), raw.decode("utf-8", errors="replace")
