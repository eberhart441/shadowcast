"""Round broadcast format v2: encode and decode the bytes a transceiver sends once per round.

The format is defined in docs/round-broadcast.md; this module is the host's copy of its layout, pinned to the
golden byte fixtures in tests/fixtures/round_broadcast_v2.json, which the firmware must reproduce too. Change the
three together and bump ``FORMAT_VERSION`` when the layout or the meaning of a field changes. v2 has v1's layout;
its authentication tag is real, computed and checked by ``csi/round_auth.py``, so this codec carries the tag
bytes as they are and does not verify them.

    broadcast = decode_broadcast(data)     # RoundBroadcast, or BroadcastError with a .reason
    data = encode_broadcast(broadcast)

A round broadcast is a 26-byte round header and one 4-byte link report per other member of the round, in
ascending node index. Little-endian throughout. The round scheduler's join frames (a join request and a join
grant, frame types 2 and 3) are the round header alone; they decode to a ``RoundBroadcast`` with no reports.

Raw mode (ticket #17) adds two frame types. A **round broadcast with raw CSI** (5) is a round broadcast whose link
reports are followed by one or two raw CSI blocks (``RoundBroadcast.raw``): the int8 I/Q of the last round
broadcast the sender heard from that transmitter. A **raw mode request** (4) is the round header (sent by a
gateway, signed by the host) followed by the target node, the target's round number and the transmitters whose
links it is to stream (``RoundBroadcast.request``); it carries no link reports. A **maintenance command** (8, OTA,
``csi/ota.py``) decodes here to its header alone, so the host can count one a gateway hears.

The score byte is the score code of ``csi/score_code.py`` (ADR-0021; ``firmware/core/motion_score.h`` on the node):
0 is no score, code c >= 1 is 2 ** ((c - 1) / 10 - 10): ``encode_score`` makes one, ``LinkReport.score`` reads it.
"""
from __future__ import annotations

import struct
from typing import NamedTuple, Optional

from .score_code import decode_score

FORMAT_VERSION = 2               # v1 had the same layout with an unchecked, zero tag
TAG_OFFSET = 18                  # the authentication tag is header bytes 18..25
FRAME_ROUND = 1                  # a round broadcast carrying link reports
FRAME_JOIN_REQUEST = 2           # a node asking to join the round, in the join slot (header only)
FRAME_JOIN_GRANT = 3             # the scheduler admitting it, in the join slot (header only)
FRAME_RAW_REQUEST = 4           # the host asking one node to stream raw CSI for some of its links (sent by a gateway)
FRAME_ROUND_RAW = 5             # a round broadcast with raw CSI blocks after its link reports
FRAME_MAINTENANCE = 8           # a maintenance command (OTA, csi/ota.py decodes its body); the host only counts it
FRAME_TYPES = frozenset({FRAME_ROUND, FRAME_JOIN_REQUEST, FRAME_JOIN_GRANT, FRAME_RAW_REQUEST, FRAME_ROUND_RAW,
                         FRAME_MAINTENANCE})
JOIN_FRAMES = frozenset({FRAME_JOIN_REQUEST, FRAME_JOIN_GRANT})
REPORT_FRAMES = frozenset({FRAME_ROUND, FRAME_ROUND_RAW})   # frames that are a node's round broadcast
MAX_NODES = 32                   # the membership bitmap is 32 bits
_HEADER = struct.Struct("<BBHBBBBI6s8s")
_REPORT = struct.Struct("<BbBB")
HEADER_BYTES = _HEADER.size      # 26
REPORT_BYTES = _REPORT.size      # 4
TAG_BYTES = 8
NODE_ID_BYTES = 6
RSSI_NONE = -128                 # rssi byte meaning "no RSSI": the transmitter was never heard
HEARD_WINDOW_ROUNDS = 15         # the "heard" nibble counts round broadcasts heard over this many rounds
SINCE_MAX = 15                   # the "since" nibble saturates: 15 means 15 or more rounds, or never

# link report byte 3: flags in bits 0-2, the slot lateness code in bits 3-7
FLAG_WARMING = 0x01              # the receiving node's per-link score is still warming up
FLAG_POSSIBLE_MOVE = 0x02        # the receiving node suspects a node moved (its baseline shifted)
FLAG_RAW_MODE = 0x04             # raw mode is active on this link
FLAGS_KNOWN = FLAG_WARMING | FLAG_POSSIBLE_MOVE | FLAG_RAW_MODE
LATENESS_SHIFT = 3
LATENESS_MAX_CODE = 31           # code c >= 1: the broadcast started (c - 1) x 32 to c x 32 us into its slot
LATENESS_UNIT_US = 32            # 31 means 960 us or more; 0 means not measured
_RAW_HEAD = struct.Struct("<BHbB")   # transmitter index, its round number, rssi, I/Q byte count
RAW_BLOCK_HEAD_BYTES = _RAW_HEAD.size   # 5
RAW_IQ_MAX_BYTES = 128           # 64 subcarriers x (imaginary, real): one HT20 long training field
RAW_MAX_LINKS = 2                # raw CSI blocks one round broadcast may carry (per node)
RAW_BLOCK_MAX_BYTES = RAW_BLOCK_HEAD_BYTES + RAW_IQ_MAX_BYTES   # 133
_REQUEST_HEAD = struct.Struct("<6sHB")   # target node id, target round, number of transmitters
REQUEST_HEAD_BYTES = _REQUEST_HEAD.size  # 9
RAW_REQUEST_MAX_AGE_ROUNDS = 64  # a node takes a raw mode request only within this many rounds of its target round
DECODE_REASONS = ("short", "version", "frame_type", "length", "membership")   # every BroadcastError.reason



class BroadcastError(ValueError):
    """Bytes that are not a v2 round broadcast. ``reason`` names why, for the rejection counters:
    short, version, frame_type, length or membership."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class LinkReport(NamedTuple):
    """What the sending node measured on one link: itself receiving from ``transmitter_index``."""
    transmitter_index: int       # node index of the transmitting node (not in the bytes: from the membership)
    score_q: int                 # quantised score, u8; 0 = no score yet
    rssi: Optional[int]          # dBm of the last round broadcast heard from it; None if never heard
    heard: int                   # round broadcasts heard from it in the last HEARD_WINDOW_ROUNDS rounds, 0..15
    since: int                   # rounds since it was last heard, 0..15 (15: 15 or more, or never)
    flags: int = 0
    lateness: int = 0            # slot lateness code of the transmitter's round broadcast, 0 = not measured

    @property
    def slot_lateness_us(self) -> Optional[int]:
        """How far into its slot the transmitter's last round broadcast started on air, by the receiving node's
        schedule, in microseconds: the middle of the code's 32 us step (960 for code 31, "960 or more"); None
        when not measured."""
        if not self.lateness:
            return None
        if self.lateness >= LATENESS_MAX_CODE:
            return LATENESS_MAX_CODE * LATENESS_UNIT_US - LATENESS_UNIT_US
        return (self.lateness - 1) * LATENESS_UNIT_US + LATENESS_UNIT_US // 2

    @property
    def score(self) -> Optional[float]:
        return decode_score(self.score_q)

    @property
    def warming(self) -> bool:
        return bool(self.flags & FLAG_WARMING)

    @property
    def possible_move(self) -> bool:
        return bool(self.flags & FLAG_POSSIBLE_MOVE)

    @property
    def raw_mode(self) -> bool:
        return bool(self.flags & FLAG_RAW_MODE)


class RawCsi(NamedTuple):
    """One raw CSI block of a round broadcast with raw CSI: what the sender measured on the last round broadcast it
    heard from ``transmitter_index``, the I/Q bytes exactly as its Wi-Fi driver gave them."""
    transmitter_index: int       # node index of the transmitting node, as in the link reports
    round: int                   # the transmitter's round number of the round broadcast the CSI was measured on
    rssi: Optional[int]          # dBm of that round broadcast; None if not known
    iq: bytes                    # int8 pairs per subcarrier, imaginary then real (ESP-IDF order), 2..128 bytes

    @property
    def values(self) -> list[int]:
        """The I/Q bytes as signed int8 values, in order."""
        return list(struct.unpack(f"<{len(self.iq)}b", self.iq))


class RawModeRequest(NamedTuple):
    """The body of a raw mode request: ``target`` is to stream raw CSI for its links from ``transmitters`` (none:
    raw mode off). ``target_round`` is the target's own round number when the host asked; the target ignores a
    request whose target round is not within its last RAW_REQUEST_MAX_AGE_ROUNDS rounds."""
    target: str                  # board serial of the receiving node, "AA:BB:CC:DD:EE:FF"
    target_round: int
    transmitters: tuple[str, ...]


class RoundBroadcast(NamedTuple):
    version: int
    frame_type: int
    round: int                   # u16, wraps
    slot: int                    # slot this broadcast was sent in
    slots_per_round: int
    schedule_version: int        # u8; node indices are only meaningful within one schedule version
    node_index: int              # the sender's index in the membership bitmap, 0..31
    membership: int              # u32 bitmap: bit i set when node index i is a member of the round
    node_id: str                 # the sender's board serial (base MAC), "AA:BB:CC:DD:EE:FF"
    tag: bytes                   # 8-byte authentication tag (csi/round_auth.py)
    reports: tuple[LinkReport, ...]
    raw: tuple[RawCsi, ...] = ()            # frame type 5 only
    request: Optional[RawModeRequest] = None   # frame type 4 only

    @property
    def carries_reports(self) -> bool:
        """A node's round broadcast (with or without raw CSI), as opposed to a join frame or a raw mode request."""
        return self.frame_type in REPORT_FRAMES

    @property
    def is_join_request(self) -> bool:
        return self.frame_type == FRAME_JOIN_REQUEST

    @property
    def is_join_grant(self) -> bool:
        return self.frame_type == FRAME_JOIN_GRANT


def report_indices(membership: int, node_index: int) -> list[int]:
    """The transmitter index of each link report, in order: every member but the sender, ascending."""
    return [i for i in range(MAX_NODES) if membership >> i & 1 and i != node_index]


def decode_broadcast(data: bytes) -> RoundBroadcast:
    if len(data) < HEADER_BYTES:
        raise BroadcastError("short", f"{len(data)} bytes is shorter than the {HEADER_BYTES}-byte round header")
    (version, frame_type, round_no, slot, slots, schedule, node_index, membership, node_id,
     tag) = _HEADER.unpack_from(data)
    if version != FORMAT_VERSION:
        raise BroadcastError("version", f"format version {version}, expected {FORMAT_VERSION}")
    if frame_type not in FRAME_TYPES:
        raise BroadcastError("frame_type", f"unknown frame type {frame_type}")
    if frame_type in JOIN_FRAMES:
        return _decode_join(data, version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                            node_id, tag)
    if frame_type == FRAME_RAW_REQUEST:
        return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                              _format_id(node_id), bytes(tag), (), (), _decode_request(data))
    if frame_type == FRAME_MAINTENANCE:     # the header alone here; csi.ota.decode_command reads the body
        return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                              _format_id(node_id), bytes(tag), ())
    if node_index >= MAX_NODES or not membership >> node_index & 1:
        raise BroadcastError("membership", f"node index {node_index} is not in membership {membership:#010x}")
    indices = report_indices(membership, node_index)
    expected = HEADER_BYTES + REPORT_BYTES * len(indices)
    if len(data) < expected or (frame_type == FRAME_ROUND and len(data) != expected):
        raise BroadcastError("length", f"{len(data)} bytes; membership {membership:#010x} needs {expected}")
    raw = _decode_raw(data, expected, set(indices)) if frame_type == FRAME_ROUND_RAW else ()
    reports = []
    for k, index in enumerate(indices):
        score_q, rssi, counts, flags = _REPORT.unpack_from(data, HEADER_BYTES + REPORT_BYTES * k)
        reports.append(LinkReport(index, score_q, None if rssi == RSSI_NONE else rssi, counts >> 4, counts & 0x0F,
                                  flags & FLAGS_KNOWN, flags >> LATENESS_SHIFT))
    return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                          _format_id(node_id), bytes(tag), tuple(reports), raw)


def _decode_raw(data: bytes, offset: int, transmitters: set[int]) -> tuple[RawCsi, ...]:
    """The raw CSI blocks that fill data[offset:] exactly: 1 to RAW_MAX_LINKS, each from a member other than the
    sender, with an even I/Q count of 2 to RAW_IQ_MAX_BYTES."""
    blocks = []
    while offset < len(data):
        if len(blocks) == RAW_MAX_LINKS:
            raise BroadcastError("length", f"more than {RAW_MAX_LINKS} raw CSI blocks")
        if offset + RAW_BLOCK_HEAD_BYTES > len(data):
            raise BroadcastError("length", "a raw CSI block is cut short")
        index, round_no, rssi, n = _RAW_HEAD.unpack_from(data, offset)
        if not (2 <= n <= RAW_IQ_MAX_BYTES and n % 2 == 0):
            raise BroadcastError("length", f"a raw CSI block of {n} I/Q bytes (an even 2 to {RAW_IQ_MAX_BYTES})")
        start = offset + RAW_BLOCK_HEAD_BYTES
        if start + n > len(data):
            raise BroadcastError("length", "a raw CSI block is cut short")
        if index not in transmitters:
            raise BroadcastError("membership", f"raw CSI for node index {index}, which has no link to the sender")
        blocks.append(RawCsi(index, round_no, None if rssi == RSSI_NONE else rssi, bytes(data[start:start + n])))
        offset = start + n
    if not blocks:
        raise BroadcastError("length", "a round broadcast with raw CSI carries no raw CSI block")
    return tuple(blocks)


def _decode_request(data: bytes) -> RawModeRequest:
    """The body of a raw mode request: target, target round, 0 to RAW_MAX_LINKS transmitter node ids."""
    if len(data) < HEADER_BYTES + REQUEST_HEAD_BYTES:
        raise BroadcastError("length", f"{len(data)} bytes is too short for a raw mode request")
    target, target_round, n = _REQUEST_HEAD.unpack_from(data, HEADER_BYTES)
    expected = HEADER_BYTES + REQUEST_HEAD_BYTES + NODE_ID_BYTES * n
    if n > RAW_MAX_LINKS or len(data) != expected:
        raise BroadcastError("length", f"{len(data)} bytes; a raw mode request for {n} link(s) is {expected} "
                                       f"(at most {RAW_MAX_LINKS} links)")
    start = HEADER_BYTES + REQUEST_HEAD_BYTES
    ids = tuple(_format_id(data[start + NODE_ID_BYTES * k:start + NODE_ID_BYTES * (k + 1)]) for k in range(n))
    return RawModeRequest(_format_id(target), target_round, ids)


def _decode_join(data: bytes, *fields) -> RoundBroadcast:
    """A join request (node index: the index asked for) or grant (node index: the index given, a member of the
    new membership): the round header alone."""
    version, frame_type, round_no, slot, slots, schedule, node_index, membership, node_id, tag = fields
    if node_index >= MAX_NODES:
        raise BroadcastError("membership", f"node index {node_index} is out of range")
    if frame_type == FRAME_JOIN_GRANT and not membership >> node_index & 1:
        raise BroadcastError("membership", f"granted node index {node_index} is not in membership {membership:#010x}")
    if len(data) != HEADER_BYTES:
        raise BroadcastError("length", f"{len(data)} bytes; a join frame is the {HEADER_BYTES}-byte header alone")
    return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                          _format_id(node_id), bytes(tag), ())


def encode_broadcast(broadcast: RoundBroadcast) -> bytes:
    """The bytes of ``broadcast``. Its reports must follow the membership (``report_indices``); a join frame has
    none."""
    if broadcast.frame_type == FRAME_MAINTENANCE:
        raise ValueError("a maintenance command is built by csi.ota.maintenance_command, not encode_broadcast")
    indices = report_indices(broadcast.membership, broadcast.node_index) if broadcast.carries_reports else []
    if [r.transmitter_index for r in broadcast.reports] != indices:
        raise ValueError(f"reports must be for node indices {indices} in that order, got "
                         f"{[r.transmitter_index for r in broadcast.reports]}")
    if len(broadcast.tag) != TAG_BYTES:
        raise ValueError(f"tag must be {TAG_BYTES} bytes")
    out = bytearray(_HEADER.pack(broadcast.version, broadcast.frame_type, broadcast.round, broadcast.slot,
                                 broadcast.slots_per_round, broadcast.schedule_version, broadcast.node_index,
                                 broadcast.membership, _parse_id(broadcast.node_id), broadcast.tag))
    for r in broadcast.reports:
        if not (0 <= r.heard <= 15 and 0 <= r.since <= 15):
            raise ValueError(f"heard and since are 4-bit counts, got {r.heard} and {r.since}")
        if not (0 <= r.flags <= FLAGS_KNOWN and 0 <= r.lateness <= LATENESS_MAX_CODE):
            raise ValueError(f"flags are 3 bits and the lateness code 5 bits, got {r.flags} and {r.lateness}")
        out += _REPORT.pack(r.score_q, RSSI_NONE if r.rssi is None else r.rssi, r.heard << 4 | r.since,
                            r.flags | r.lateness << LATENESS_SHIFT)
    if broadcast.frame_type == FRAME_ROUND_RAW:
        out += _encode_raw(broadcast.raw, set(indices))
    elif broadcast.raw:
        raise ValueError("only a round broadcast with raw CSI (frame type 5) carries raw CSI blocks")
    if broadcast.frame_type == FRAME_RAW_REQUEST:
        out += _encode_request(broadcast.request)
    return bytes(out)


def _encode_raw(blocks: tuple[RawCsi, ...], transmitters: set[int]) -> bytes:
    if not 1 <= len(blocks) <= RAW_MAX_LINKS:
        raise ValueError(f"a round broadcast with raw CSI carries 1 to {RAW_MAX_LINKS} blocks, got {len(blocks)}")
    out = bytearray()
    for c in blocks:
        n = len(c.iq)
        if not (2 <= n <= RAW_IQ_MAX_BYTES and n % 2 == 0):
            raise ValueError(f"a raw CSI block holds an even 2 to {RAW_IQ_MAX_BYTES} I/Q bytes, got {n}")
        if c.transmitter_index not in transmitters:
            raise ValueError(f"raw CSI for node index {c.transmitter_index}, which has no link to the sender")
        out += _RAW_HEAD.pack(c.transmitter_index, c.round, RSSI_NONE if c.rssi is None else c.rssi, n) + c.iq
    return bytes(out)


def _encode_request(request: Optional[RawModeRequest]) -> bytes:
    if request is None:
        raise ValueError("a raw mode request (frame type 4) needs its request")
    if len(request.transmitters) > RAW_MAX_LINKS:
        raise ValueError(f"a raw mode request names at most {RAW_MAX_LINKS} transmitters")
    return (_REQUEST_HEAD.pack(_parse_id(request.target), request.target_round, len(request.transmitters))
            + b"".join(_parse_id(t) for t in request.transmitters))


def _format_id(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def _parse_id(node_id: str) -> bytes:
    raw = bytes.fromhex(node_id.replace(":", ""))
    if len(raw) != NODE_ID_BYTES:
        raise ValueError(f"node id must be {NODE_ID_BYTES} bytes, got {node_id!r}")
    return raw
