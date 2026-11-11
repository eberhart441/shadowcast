"""Round broadcasts in, placed link reports out: the host's ingest for the transceiver pipeline.

Every transceiver source is a stream of ``(host time, round broadcast bytes)`` (``BroadcastStream``; see
docs/adr/0022-host-time-round-broadcast-seam.md). ``RoundIngest`` decodes each round broadcast
(csi/broadcast.py), names its sender by board serial from the room config, and places each link report on its
link, ``NodeLinkId(receiving node, transmitting node)``:

    ingest = RoundIngest(room)
    for link_id, report in ingest.place(data, host_ts, source="gw1"): ...
    ingest.count_dropped("gw1", n)   # records the source's queue dropped before they got here
    ingest.counts()     # see counts()

Several gateways may hear the same round broadcast. The first copy of (node serial, schedule version, round
number) is placed; another copy within ``DEDUP_WINDOW_S`` of host time, from any source, is counted as a
``duplicate`` and not placed again. The window is far shorter than the u16 round number takes to wrap.

A link report names its transmitter only by node index. The index of a node is learned from that node's own
round broadcasts, per schedule version, so a report about a node the host has not heard from yet (this schedule
version) cannot be placed: it is counted as unplaced. Nothing is dropped without being counted: every record a
source delivered is accepted, rejected for one of ``REJECT_REASONS`` or from an unknown node, so
``heard == accepted + sum(rejected) + sum(unknown_nodes)``. Memory is bounded: the de-duplication memory by
time and ``DEDUP_MAX``, the unknown serials by ``UNKNOWN_NODES_KEPT``.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Iterable, Optional

from .broadcast import DECODE_REASONS, BroadcastError, LinkReport, decode_broadcast
from .links import NodeLinkId
from .room import Room

# A stream of (host time, round broadcast bytes); a quiet source may yield (host_ts, None) as a heartbeat.
BroadcastStream = Iterable[tuple[float, Optional[bytes]]]
SCHEDULES_KEPT = 4   # schedule versions whose node indices are remembered
DEDUP_WINDOW_S = 2.0  # host time within which a second copy of (node, schedule version, round) is a duplicate
DEDUP_MAX = 8192      # round broadcasts remembered for de-duplication (32 nodes x 40 Hz x 2 s = 2560)
DUPLICATE = "duplicate"
QUEUE_OVERFLOW = "queue_overflow"
UNKNOWN_NODES_KEPT = 32   # unknown serials counted by name; any further ones are counted together as OTHER_NODES
OTHER_NODES = "other"
REJECT_REASONS = DECODE_REASONS + (DUPLICATE, QUEUE_OVERFLOW)   # each has a counter in status.broadcasts.rejected


@dataclass(frozen=True)
class HeardBroadcast:
    """One round broadcast as a source delivered it: the bytes and the host time they were heard at."""
    host_ts: float
    data: bytes


class RoundIngest:
    def __init__(self, room: Room) -> None:
        self._names = {node.serial.upper(): node.name for node in room.transceivers if node.serial}
        self._index: dict[int, dict[int, str]] = {}   # schedule version -> {node index: node name}
        self._seen: OrderedDict[tuple[str, int, int], float] = OrderedDict()  # (serial, schedule, round) -> host_ts
        self._heard = 0
        self._accepted = 0
        self._rejected: Counter[str] = Counter({reason: 0 for reason in REJECT_REASONS})
        self._unknown: Counter[str] = Counter()
        self._unplaced = 0
        self._sources: dict[str, Counter[str]] = {}

    def place(self, data: bytes, host_ts: float = 0.0, source: str = "") -> list[tuple[NodeLinkId, LinkReport]]:
        """The link reports of one round broadcast heard by ``source`` at ``host_ts``, each on its link.

        Rejected bytes, a copy of a round broadcast already placed (another gateway heard it too) and unknown
        senders give [] and are counted."""
        tally = self._source(source)
        self._heard += 1
        tally["heard"] += 1
        try:
            broadcast = decode_broadcast(data)
        except BroadcastError as e:
            self._rejected[e.reason] += 1
            return []
        serial = broadcast.node_id.upper()
        if self._duplicate((serial, broadcast.schedule_version, broadcast.round), host_ts):
            self._rejected[DUPLICATE] += 1
            tally[DUPLICATE] += 1
            return []
        receiver = self._names.get(serial)
        if receiver is None:
            known = broadcast.node_id in self._unknown or len(self._unknown) < UNKNOWN_NODES_KEPT
            self._unknown[broadcast.node_id if known else OTHER_NODES] += 1
            return []
        self._accepted += 1
        tally["accepted"] += 1
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

    def count_dropped(self, source: str, n: int) -> None:
        """``n`` round broadcasts heard by ``source`` were dropped before they reached the ingest (queue full)."""
        tally = self._source(source)
        tally["heard"] += n
        tally[QUEUE_OVERFLOW] += n
        self._heard += n
        self._rejected[QUEUE_OVERFLOW] += n

    def _duplicate(self, key: tuple[str, int, int], host_ts: float) -> bool:
        """True if ``key`` was seen within DEDUP_WINDOW_S of ``host_ts``; else remember it. Bounded in time and size."""
        seen = self._seen
        while seen and (len(seen) >= DEDUP_MAX or next(iter(seen.values())) < host_ts - DEDUP_WINDOW_S):
            seen.popitem(last=False)
        first = seen.get(key)
        if first is not None and abs(host_ts - first) <= DEDUP_WINDOW_S:
            return True
        seen.pop(key, None)
        seen[key] = host_ts
        return False

    def _source(self, name: str) -> Counter[str]:
        tally = self._sources.get(name)
        if tally is None:
            tally = self._sources[name] = Counter({"heard": 0, "accepted": 0, DUPLICATE: 0, QUEUE_OVERFLOW: 0})
        return tally

    def counts(self) -> dict:
        """``status.broadcasts``: heard (records delivered, including those dropped on a full queue), accepted
        (placed), rejected {reason: n} for every one of REJECT_REASONS, unknown_nodes {serial: n} (beyond
        UNKNOWN_NODES_KEPT serials, the rest under "other"), unplaced_reports, and per source
        {heard, accepted, duplicate, queue_overflow}."""
        return {"heard": self._heard, "accepted": self._accepted, "rejected": dict(self._rejected), "unknown_nodes": dict(self._unknown),
                "unplaced_reports": self._unplaced,
                "sources": {name: dict(tally) for name, tally in self._sources.items()}}
