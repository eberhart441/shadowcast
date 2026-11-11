"""Round broadcasts in, placed link reports out: the host's ingest for the transceiver pipeline.

Every transceiver source is a stream of ``(host time, round broadcast bytes)`` (``BroadcastStream``; see
docs/adr/0022-host-time-round-broadcast-seam.md). ``RoundIngest`` decodes each round broadcast
(csi/broadcast.py), checks its authentication tag against the kit key and its round number
against replay (csi/round_auth.py), names its sender by board serial from the room config, and places each link
report on its link, ``NodeLinkId(receiving node, transmitting node)``:

    ingest = RoundIngest(room, kit_key)        # kit_key defaults to the simulator's TEST_KIT_KEY
    for link_id, report in ingest.place(data, host_ts, source="gw1"): ...
    ingest.count_dropped("gw1", n)   # records the source's queue dropped before they got here
    ingest.last_node    # the sender of the round broadcast just placed, None if it was not placed
    ingest.last_raw     # its raw CSI blocks, each on its link (raw mode, ticket #17)
    ingest.last_rounds  # {board serial: round number of its latest accepted round broadcast}
    ingest.counts()     # see counts()

A broadcast whose tag does not verify (forged, or another kit's key) is counted as ``auth`` before anything else
looks at it, so a forgery never enters the de-duplication memory and cannot crowd out the real broadcast. One that
verifies but whose round number is stale for its node (``round_auth.Freshness``) is counted as ``replay``.

Several gateways may hear the same round broadcast. The first copy of (node serial, schedule version, round
number) is placed; another copy within ``DEDUP_WINDOW_S`` of host time, from any source, is counted as a
``duplicate`` and not placed again. The window is far shorter than the u16 round number takes to wrap.

A link report names its transmitter only by node index. The index of a node is learned from that node's own
round broadcasts, per schedule version, so a report about a node the host has not heard from yet (this schedule
version) cannot be placed: it is counted as unplaced. A join grant from the round scheduler (ticket #14) names
the joining node and its index in the new schedule version, so reports about a joiner place from the start.
Join frames (requests and grants) carry no link reports: each copy is counted in ``join_frames`` and never
placed, de-duplicated or rejected. A round broadcast with raw CSI (frame type 5) is placed like any round
broadcast, and its raw CSI blocks are placed on their links too (``last_raw``, counted in ``raw_csi``). A raw mode
request (frame type 4) that a gateway heard from another gateway is counted in ``raw_requests`` and not used, and
so is an authenticated maintenance command (frame type 8, OTA) in ``maintenance_commands``.
Nothing is dropped without being counted: every record a source delivered is accepted, rejected for one of
``REJECT_REASONS``, from an unknown node, a join frame, a raw mode request or a maintenance command, so
``heard == accepted + sum(rejected) + sum(unknown_nodes) + sum(join_frames) + raw_requests + maintenance_commands``. Memory is bounded: the de-duplication memory by
time and ``DEDUP_MAX``, the unknown serials by ``UNKNOWN_NODES_KEPT``.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Iterable, Optional

from .broadcast import (DECODE_REASONS, FRAME_JOIN_GRANT, FRAME_MAINTENANCE, FRAME_RAW_REQUEST, JOIN_FRAMES, BroadcastError, LinkReport,
                        RawCsi, decode_broadcast)
from .links import NodeLinkId
from .room import Room
from .round_auth import REPLAY, TEST_KIT_KEY, Freshness, tag_ok

# A stream of (host time, round broadcast bytes); a quiet source may yield (host_ts, None) as a heartbeat.
BroadcastStream = Iterable[tuple[float, Optional[bytes]]]
SCHEDULES_KEPT = 4   # schedule versions whose node indices are remembered
DEDUP_WINDOW_S = 2.0  # host time within which a second copy of (node, schedule version, round) is a duplicate
DEDUP_MAX = 8192      # round broadcasts remembered for de-duplication (32 nodes x 40 Hz x 2 s = 2560)
AUTH = "auth"          # the tag does not verify under the kit key
DUPLICATE = "duplicate"
QUEUE_OVERFLOW = "queue_overflow"
UNKNOWN_NODES_KEPT = 32   # unknown serials counted by name; any further ones are counted together as OTHER_NODES
OTHER_NODES = "other"
REJECT_REASONS = DECODE_REASONS + (AUTH, REPLAY, DUPLICATE, QUEUE_OVERFLOW)   # each has a counter in status.broadcasts.rejected
JOIN_FRAME_KINDS = ("request", "grant")                          # status.broadcasts.join_frames


@dataclass(frozen=True)
class HeardBroadcast:
    """One round broadcast as a source delivered it: the bytes and the host time they were heard at."""
    host_ts: float
    data: bytes


class RoundIngest:
    def __init__(self, room: Room, kit_key: bytes = TEST_KIT_KEY) -> None:
        self._key = bytes(kit_key)
        self._fresh = Freshness()
        self._names = {node.serial.upper(): node.name for node in room.transceivers if node.serial}
        self._index: dict[int, dict[int, str]] = {}   # schedule version -> {node index: node name}
        self._seen: OrderedDict[tuple[str, int, int], float] = OrderedDict()  # (serial, schedule, round) -> host_ts
        self._heard = 0
        self._accepted = 0
        self._rejected: Counter[str] = Counter({reason: 0 for reason in REJECT_REASONS})
        self._unknown: Counter[str] = Counter()
        self._unplaced = 0
        self._joins: Counter[str] = Counter({kind: 0 for kind in JOIN_FRAME_KINDS})
        self._sources: dict[str, Counter[str]] = {}
        self._raw_requests = 0
        self._maintenance = 0
        self._last_source: dict[str, str] = {}   # node name -> the source of its latest accepted round broadcast
        self._raw = Counter({"blocks": 0, "iq_bytes": 0, "unplaced": 0})
        self.last_node: Optional[str] = None   # sender of the round broadcast just placed (None: not placed)
        self.last_raw: list[tuple[NodeLinkId, RawCsi]] = []   # its raw CSI blocks, each on its link
        self.last_rounds: dict[str, int] = {}  # board serial (upper case) -> round of its latest accepted broadcast

    def place(self, data: bytes, host_ts: float = 0.0, source: str = "") -> list[tuple[NodeLinkId, LinkReport]]:
        """The link reports of one round broadcast heard by ``source`` at ``host_ts``, each on its link.

        Rejected bytes, a copy of a round broadcast already placed (another gateway heard it too) and unknown
        senders give [] and are counted."""
        self.last_node = None
        self.last_raw = []
        tally = self._source(source)
        self._heard += 1
        tally["heard"] += 1
        try:
            broadcast = decode_broadcast(data)
        except BroadcastError as e:
            self._rejected[e.reason] += 1
            return []
        if not tag_ok(data, self._key):
            self._rejected[AUTH] += 1
            return []
        if broadcast.frame_type in JOIN_FRAMES:
            self._join(broadcast)
            return []
        if broadcast.frame_type == FRAME_RAW_REQUEST:
            self._raw_requests += 1    # another gateway's request to a node: nothing for the host to use
            return []
        if broadcast.frame_type == FRAME_MAINTENANCE:
            self._maintenance += 1     # an OTA maintenance command a gateway sent: counted, not used
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
        if self._fresh.check(serial, broadcast.round, host_ts) == REPLAY:
            self._rejected[REPLAY] += 1
            return []
        self._accepted += 1
        tally["accepted"] += 1
        self.last_node = receiver
        self.last_rounds[serial] = broadcast.round
        self._last_source[receiver] = source
        indices = self._indices(broadcast.schedule_version)
        indices[broadcast.node_index] = receiver
        placed = []
        for report in broadcast.reports:
            transmitter = indices.get(report.transmitter_index)
            if transmitter is None or transmitter == receiver:
                self._unplaced += 1
                continue
            placed.append((NodeLinkId(receiver, transmitter), report))
        for block in broadcast.raw:
            transmitter = indices.get(block.transmitter_index)
            if transmitter is None or transmitter == receiver:
                self._raw["unplaced"] += 1
                continue
            self._raw["blocks"] += 1
            self._raw["iq_bytes"] += len(block.iq)
            self.last_raw.append((NodeLinkId(receiver, transmitter), block))
        return placed

    def last_source(self, name: str) -> Optional[str]:
        """The source that delivered node ``name``'s latest accepted round broadcast (None: never heard)."""
        return self._last_source.get(name)

    def _join(self, frame) -> None:
        """A join request or grant: counted; a grant for a known node teaches its index in the new schedule."""
        grant = frame.frame_type == FRAME_JOIN_GRANT
        self._joins["grant" if grant else "request"] += 1
        name = self._names.get(frame.node_id.upper())
        if grant and name is not None:
            self._indices(frame.schedule_version)[frame.node_index] = name

    def _indices(self, schedule_version: int) -> dict[int, str]:
        indices = self._index.setdefault(schedule_version, {})
        while len(self._index) > SCHEDULES_KEPT:
            del self._index[next(iter(self._index))]
        return indices

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
        UNKNOWN_NODES_KEPT serials, the rest under "other"), unplaced_reports, join_frames {request, grant}
        (every copy), raw_requests (raw mode requests heard, every copy), maintenance_commands (OTA maintenance
        commands heard, every copy), raw_csi {blocks, iq_bytes, unplaced}
        (raw CSI blocks of accepted round broadcasts), and per source {heard, accepted, duplicate,
        queue_overflow}."""
        return {"heard": self._heard, "accepted": self._accepted, "rejected": dict(self._rejected), "unknown_nodes": dict(self._unknown),
                "unplaced_reports": self._unplaced, "join_frames": dict(self._joins),
                "raw_requests": self._raw_requests, "maintenance_commands": self._maintenance,
                "raw_csi": dict(self._raw),
                "sources": {name: dict(tally) for name, tally in self._sources.items()}}
