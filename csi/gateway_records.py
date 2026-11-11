"""The node gateway's USB byte stream as a transceiver source: gateway records in, (host time, round broadcast) out.

A gateway node (firmware/node, role gateway) listens on the sensing channel and writes every ESP-NOW frame it hears
to USB as a framed binary record with its receive time (docs/gateway-record.md). ``GatewaySource`` turns that byte
stream into the transceiver pipeline's input seam, ``(host time, round broadcast bytes)`` (ADR-0022), so a gateway
is just another named stream for ``DashboardServer``:

    DashboardServer(room, {"gateway": gateway_source("/dev/ttyACM0")}, mode="auto")

The payload of a heard record is passed on undecoded: ``RoundIngest`` decodes it and counts what it rejects. Status
and text records are kept on the source (``status``, ``texts``). Bytes that are not a record (the boot ROM's text)
are skipped and counted; a record that fails its checks is counted by reason and costs only itself, because the
reader resynchronises on the next sync bytes. Nothing is dropped without a count (``counts()``).

Host time: the records completed by one USB read share its arrival time, so each is placed by the gateway's own
receive time relative to the newest of them. The order and spacing of the round broadcasts survive USB batching.

Sending (raw mode, ticket #17): ``send_frame(frame)`` writes the text command ``send <hex>`` to the gateway, which
sends that frame (a raw mode request the host signed) in the join slot of the next round its scheduler numbers
even. ``gateway_serial`` is the gateway's board serial from its status record (None before the first one, or when
the board on the port is not a gateway), the node id the host puts in the frames it sends this way.
"""
from __future__ import annotations

import binascii
import struct
import threading
import time
from collections import Counter, deque
from typing import Iterable, Iterator, NamedTuple, Optional, Union

SYNC = b"SC"
VERSION = 1
TYPE_HEARD, TYPE_STATUS, TYPE_TEXT = 1, 2, 3
TYPE_NAMES = {TYPE_HEARD: "heard", TYPE_STATUS: "status", TYPE_TEXT: "text"}
HEAD_BYTES = 6                    # sync, version, type, u16 body length
FRAME_BYTES = HEAD_BYTES + 2      # plus the CRC
MAX_BODY = 1500
_HEARD = struct.Struct("<QbB")    # receive time (gateway device time, us), rssi, reserved
_STATUS = struct.Struct("<6sQBBBB")
ROLES = {0: "transceiver", 1: "gateway"}
COUNTER_NAMES = ("heard", "queue_full", "usb_dropped", "sent", "send_errors", "rejected", "csi_skipped")
LOSS_COUNTERS = ("queue_full", "usb_dropped", "csi_skipped")   # status counters of frames the gateway lost
MAX_SPREAD_S = 2.0                # records of one read are never placed more than this before its arrival
TEXTS_KEPT = 50
WRITE_TIMEOUT_S = 1.0             # a command line to the gateway that cannot be written in this long fails

Chunks = Iterable[tuple[float, Optional[bytes]]]


class RecordError(ValueError):
    """Bytes that are not a v1 gateway record. ``reason``: truncated, sync, version, too_long, crc, type, short_body."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Heard(NamedTuple):
    rx_time_us: int               # the gateway's device time when the frame arrived
    rssi: int
    payload: bytes                # the ESP-NOW body as received: a round broadcast for our nodes


class NodeStatus(NamedTuple):
    serial: str                   # the node's board serial, "AA:BB:CC:DD:EE:FF"
    uptime_us: int
    reset_reason: int
    role: str                     # "transceiver" or "gateway" ("role 7" for a role this host does not know)
    channel: int
    counters: tuple[int, ...]     # in COUNTER_NAMES order, counted since boot

    def counter(self, name: str) -> int:
        i = COUNTER_NAMES.index(name)
        return self.counters[i] if i < len(self.counters) else 0


class Text(NamedTuple):
    text: str


Record = Union[Heard, NodeStatus, Text]


def _crc(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)   # CRC-16/CCITT-FALSE


def _frame(record_type: int, body: bytes) -> bytes:
    if len(body) > MAX_BODY:
        raise ValueError(f"body of {len(body)} bytes is over the {MAX_BODY}-byte limit")
    inner = struct.pack("<BBH", VERSION, record_type, len(body)) + body
    return SYNC + inner + struct.pack("<H", _crc(inner))


def encode_heard(rx_time_us: int, rssi: int, payload: bytes) -> bytes:
    return _frame(TYPE_HEARD, _HEARD.pack(rx_time_us, rssi, 0) + payload)


def encode_status(serial: str, uptime_us: int, reset_reason: int, role: int, channel: int,
                  counters: Iterable[int]) -> bytes:
    raw = bytes.fromhex(serial.replace(":", ""))
    body = _STATUS.pack(raw, uptime_us, reset_reason, role, channel, 0) + b"".join(struct.pack("<I", c)
                                                                                     for c in counters)
    return _frame(TYPE_STATUS, body)


def encode_text(text: str) -> bytes:
    return _frame(TYPE_TEXT, text.encode())


def decode_record(data: bytes) -> tuple[Record, int]:
    """The record at the start of ``data`` and the bytes it took. Raises RecordError."""
    if data[:2] != SYNC[:len(data[:2])]:
        raise RecordError("sync", "does not start with the sync bytes")
    if len(data) < HEAD_BYTES:
        raise RecordError("truncated", f"{len(data)} bytes is shorter than a record head")
    version, record_type, body_len = struct.unpack_from("<BBH", data, 2)
    if version != VERSION:
        raise RecordError("version", f"record version {version}, expected {VERSION}")
    if body_len > MAX_BODY:
        raise RecordError("too_long", f"body length {body_len} is over {MAX_BODY}")
    length = body_len + FRAME_BYTES
    if len(data) < length:
        raise RecordError("truncated", f"{len(data)} of {length} bytes")
    (crc,) = struct.unpack_from("<H", data, HEAD_BYTES + body_len)
    if crc != _crc(bytes(data[2:HEAD_BYTES + body_len])):
        raise RecordError("crc", "CRC mismatch")
    body = bytes(data[HEAD_BYTES:HEAD_BYTES + body_len])
    if record_type == TYPE_HEARD:
        if len(body) < _HEARD.size:
            raise RecordError("short_body", f"heard body of {len(body)} bytes")
        rx_time_us, rssi, _ = _HEARD.unpack_from(body)
        return Heard(rx_time_us, rssi, body[_HEARD.size:]), length
    if record_type == TYPE_STATUS:
        if len(body) < _STATUS.size:
            raise RecordError("short_body", f"status body of {len(body)} bytes")
        raw, uptime_us, reset_reason, role, channel, _ = _STATUS.unpack_from(body)
        n = (len(body) - _STATUS.size) // 4
        counters = struct.unpack_from(f"<{n}I", body, _STATUS.size)
        return NodeStatus(":".join(f"{b:02X}" for b in raw), uptime_us, reset_reason, ROLES.get(role, f"role {role}"),
                          channel, counters), length
    if record_type == TYPE_TEXT:
        return Text(body.decode("utf-8", errors="replace")), length
    raise RecordError("type", f"unknown record type {record_type}")


class RecordReader:
    """Incremental reader: feed it bytes as they arrive, get the complete records back. Resynchronises on errors."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.records: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()
        self.skipped_bytes = 0

    def feed(self, data: bytes) -> list[Record]:
        self._buf += data
        out: list[Record] = []
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                keep = 1 if self._buf[-1:] == SYNC[:1] else 0   # a sync may be split across reads
                self.skipped_bytes += len(self._buf) - keep
                del self._buf[:len(self._buf) - keep]
                return out
            if start:
                self.skipped_bytes += start
                del self._buf[:start]
            try:
                record, length = decode_record(self._buf)
            except RecordError as e:
                if e.reason == "truncated":
                    return out
                self.rejected[e.reason] += 1
                del self._buf[:1]              # not a record here: look for the next sync
                continue
            del self._buf[:length]
            self.records[TYPE_NAMES[_type_of(record)]] += 1
            out.append(record)

    def counts(self) -> dict:
        return {"records": dict(self.records), "rejected": dict(self.rejected), "skipped_bytes": self.skipped_bytes}


def _type_of(record: Record) -> int:
    if isinstance(record, Heard):
        return TYPE_HEARD
    if isinstance(record, NodeStatus):
        return TYPE_STATUS
    return TYPE_TEXT


class GatewaySource:
    """A BroadcastStream (csi/rounds.py) over ``chunks``, an iterable of (arrival time, bytes read or None)."""

    def __init__(self, chunks: Chunks) -> None:
        self._chunks = chunks
        self._reader = RecordReader()
        self._lock = threading.Lock()                 # the reader thread feeds; the dashboard reads losses()
        self.status: Optional[NodeStatus] = None      # the gateway's latest status record
        self.texts: deque[str] = deque(maxlen=TEXTS_KEPT)

    def __iter__(self) -> Iterator[tuple[float, Optional[bytes]]]:
        for arrived, data in self._chunks:
            heard: list[Heard] = []
            with self._lock:
                records = self._reader.feed(data) if data else ()
            for record in records:
                if isinstance(record, Heard):
                    heard.append(record)
                elif isinstance(record, NodeStatus):
                    self.status = record
                else:
                    self.texts.append(record.text)
            if not heard:
                yield arrived, None                     # a heartbeat: the port is alive
                continue
            newest = max(h.rx_time_us for h in heard)
            for h in heard:
                before = min(max((newest - h.rx_time_us) / 1e6, 0.0), MAX_SPREAD_S)
                yield arrived - before, h.payload

    def counts(self) -> dict:
        """{"records": {type: n}, "rejected": {reason: n}, "skipped_bytes": n}."""
        with self._lock:
            return self._reader.counts()

    def losses(self) -> dict:
        """What never reached the host as a round broadcast, by where it was lost: on the gateway (its status
        record's ``queue_full``, ``usb_dropped`` and ``csi_skipped``, counted since its boot; 0 before the first
        status record) and on the way in (``records_rejected`` {reason: n} for records that failed their checks,
        ``skipped_bytes`` that were not a record). The dashboard shows it per gateway in ``status.broadcasts``."""
        counts = self.counts()
        status = self.status
        out = {name: status.counter(name) if status is not None else 0 for name in LOSS_COUNTERS}
        out["records_rejected"] = counts["rejected"]
        out["skipped_bytes"] = counts["skipped_bytes"]
        return out

    @property
    def gateway_serial(self) -> Optional[str]:
        """The gateway's board serial once its status record has arrived; None before, or for a transceiver."""
        status = self.status
        return status.serial if status is not None and status.role == "gateway" else None

    def send_frame(self, frame: bytes) -> None:
        """Have the gateway send ``frame`` on the sensing channel (``send <hex>``). RuntimeError if these chunks
        cannot be written to; OSError from the port."""
        write = getattr(self._chunks, "write", None)
        if write is None:
            raise RuntimeError("this gateway source cannot send (its chunks are read-only)")
        write(f"send {bytes(frame).hex()}\n".encode())

    def close(self) -> None:
        """Close the chunk source (a serial port's reader releases the port). MultiSource calls this on stop."""
        close = getattr(self._chunks, "close", None)
        if close:
            close()


class SerialChunks:
    """A serial port's reads as ``(time.time(), bytes or None)`` chunks, as ``csi.parser.serial_chunks`` gives them,
    that can also be written to (a command line to the gateway). The port opens on the first read and closes when
    the reading ends or ``close`` is called."""

    def __init__(self, port: str, baud: int = 921600, timeout: float = 1.0) -> None:
        self._port, self._baud, self._timeout = port, baud, timeout
        self._ser = None
        self._lock = threading.Lock()

    def __iter__(self) -> Iterator[tuple[float, Optional[bytes]]]:
        from .parser import open_serial

        with self._lock:
            if self._ser is None:
                self._ser = open_serial(self._port, self._baud, self._timeout)
                self._ser.write_timeout = WRITE_TIMEOUT_S   # pyserial raises SerialTimeoutException (an OSError)
            ser = self._ser
        try:
            while True:
                chunk = ser.read(ser.in_waiting or 1)
                yield time.time(), (chunk or None)
        finally:
            self.close()

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._ser is None:
                raise OSError(f"{self._port} is not open")
            self._ser.write(data)

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                self._ser.close()
                self._ser = None


def gateway_source(port: str, baud: int = 921600) -> GatewaySource:
    """The gateway on serial ``port`` as a transceiver source that can also send. The port opens on first read."""
    return GatewaySource(SerialChunks(port, baud))
