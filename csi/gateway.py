"""Wireless receivers: split the gateway board's one serial stream back into one stream per receiver.

Receivers built with ``CSI_FORWARD=1`` send each CSI line over ESP-NOW to the gateway board (``CSI_GATEWAY=1``),
the only board on the Pi's USB. It writes them as ``RX,<receiver's USB serial>,CSI_DATA,...``.
``split_gateway`` routes each line, without its prefix, to the receiver whose ``serial`` in config/room.json
matches, so the rest of the pipeline sees the same named (host_ts, line) streams as with wired receivers.
Lines from boards not in the config, and anything that is not a forwarded line, are dropped.

A quiet receiver yields heartbeats (line None) like a quiet serial port. If the gateway's port fails, every
receiver's stream raises that error, so the dashboard reports the receivers as failed.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Iterable, Iterator, Mapping, Optional

from .parser import serial_stream
from .ports import normalise_serial
from .room import Room

PREFIX = "RX,"
HEARTBEAT_S = 1.0
QUEUE_LINES = 2000   # per receiver; a stalled consumer drops lines rather than growing without bound
_END = object()


def split_gateway(lines: Iterable[tuple[float, Optional[str]]],
                  names_by_serial: Mapping[str, str]) -> dict[str, Iterator[tuple[float, Optional[str]]]]:
    """One (host_ts, line) stream per receiver name, fed from the gateway's ``lines`` on a background thread."""
    names = {normalise_serial(serial): name for serial, name in names_by_serial.items()}
    queues = {name: queue.Queue(QUEUE_LINES) for name in names.values()}

    def pump() -> None:
        end: object = _END
        try:
            for host_ts, line in lines:
                if not line or not line.startswith(PREFIX):
                    continue
                serial, _, rest = line[len(PREFIX):].partition(",")
                name = names.get(normalise_serial(serial))
                if name is not None:
                    try:
                        queues[name].put_nowait((host_ts, rest))
                    except queue.Full:
                        pass
        except Exception as e:  # the gateway port failed: every receiver reports it
            end = e
        for q in queues.values():
            q.put(end)

    def stream(q: queue.Queue) -> Iterator[tuple[float, Optional[str]]]:
        while True:
            try:
                item = q.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                yield time.time(), None
                continue
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    threading.Thread(target=pump, name="gateway", daemon=True).start()
    return {name: stream(q) for name, q in queues.items()}


def gateway_streams(port: str, room: Room, baud: int = 921600) -> dict[str, Iterator[tuple[float, Optional[str]]]]:
    """Streams for every enabled receiver with a ``serial`` in the room, read through the gateway on ``port``."""
    names = {node.serial: node.name for node in room.receivers if node.serial}
    if not names:
        raise ValueError("--gateway needs a \"serial\" for each receiver in the room config")
    return split_gateway(serial_stream(port, baud), names)
