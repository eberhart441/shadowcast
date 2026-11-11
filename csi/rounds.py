"""Round broadcasts in, placed link reports out: the host's ingest for the transceiver pipeline.

Every transceiver source is a stream of ``(host time, round broadcast bytes)`` (``BroadcastStream``; see
docs/adr/0021-host-time-round-broadcast-seam.md). ``RoundIngest`` decodes each round broadcast
(csi/broadcast.py), names its sender by board serial from the room config, and places each link report on its
link, ``NodeLinkId(receiving node, transmitting node)``:

    ingest = RoundIngest(room)
    for link_id, report in ingest.place(data): ...
    ingest.counts()     # {"accepted", "rejected": {reason: n}, "unknown_nodes": {serial: n}, "unplaced_reports"}

A link report names its transmitter only by node index. The index of a node is learned from that node's own
round broadcasts, per schedule version, so a report about a node the host has not heard from yet (this schedule
version) cannot be placed: it is counted as unplaced. Nothing is dropped without being counted.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from .broadcast import BroadcastError, LinkReport, decode_broadcast
from .links import NodeLinkId
from .room import Room

# A stream of (host time, round broadcast bytes); a quiet source may yield (host_ts, None) as a heartbeat.
BroadcastStream = Iterable[tuple[float, Optional[bytes]]]
SCHEDULES_KEPT = 4   # schedule versions whose node indices are remembered


class RoundIngest:
    def __init__(self, room: Room) -> None:
        self._names = {node.serial.upper(): node.name for node in room.transceivers if node.serial}
        self._index: dict[int, dict[int, str]] = {}   # schedule version -> {node index: node name}
        self._accepted = 0
        self._rejected: Counter[str] = Counter()
        self._unknown: Counter[str] = Counter()
        self._unplaced = 0

    def place(self, data: bytes) -> list[tuple[NodeLinkId, LinkReport]]:
        """The link reports of one round broadcast, each on its link. Rejected bytes and unknown senders give []."""
        try:
            broadcast = decode_broadcast(data)
        except BroadcastError as e:
            self._rejected[e.reason] += 1
            return []
        receiver = self._names.get(broadcast.node_id.upper())
        if receiver is None:
            self._unknown[broadcast.node_id] += 1
            return []
        self._accepted += 1
        indices = self._index.setdefault(broadcast.schedule_version, {})
        indices[broadcast.node_index] = receiver
        while len(self._index) > SCHEDULES_KEPT:
            del self._index[next(iter(self._index))]
        placed = []
        for report in broadcast.reports:
            transmitter = indices.get(report.transmitter_index)
            if transmitter is None or transmitter == receiver:
                self._unplaced += 1
                continue
            placed.append((NodeLinkId(receiver, transmitter), report))
        return placed

    def counts(self) -> dict:
        return {"accepted": self._accepted, "rejected": dict(self._rejected), "unknown_nodes": dict(self._unknown),
                "unplaced_reports": self._unplaced}
