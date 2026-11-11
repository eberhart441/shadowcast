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

The score byte is the score code of ``csi/score_code.py`` (ADR-0021; ``firmware/core/motion_score.h`` on the node):
0 is no score, code c >= 1 is 2 ** ((c - 1) / 10 - 10). ``quantise_score`` and ``dequantise_score`` are its
encode and decode under the names this module uses.
"""
from __future__ import annotations

import struct
from typing import NamedTuple, Optional

from .score_code import decode_score, encode_score

FORMAT_VERSION = 2               # v1 had the same layout with an unchecked, zero tag
TAG_OFFSET = 18                  # the authentication tag is header bytes 18..25
FRAME_ROUND = 1                  # a round broadcast carrying link reports
FRAME_JOIN_REQUEST = 2           # a node asking to join the round, in the join slot (header only)
FRAME_JOIN_GRANT = 3             # the scheduler admitting it, in the join slot (header only)
FRAME_TYPES = frozenset({FRAME_ROUND, FRAME_JOIN_REQUEST, FRAME_JOIN_GRANT})
JOIN_FRAMES = frozenset({FRAME_JOIN_REQUEST, FRAME_JOIN_GRANT})
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
        return dequantise_score(self.score_q)

    @property
    def warming(self) -> bool:
        return bool(self.flags & FLAG_WARMING)

    @property
    def possible_move(self) -> bool:
        return bool(self.flags & FLAG_POSSIBLE_MOVE)

    @property
    def raw_mode(self) -> bool:
        return bool(self.flags & FLAG_RAW_MODE)


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
    if node_index >= MAX_NODES or not membership >> node_index & 1:
        raise BroadcastError("membership", f"node index {node_index} is not in membership {membership:#010x}")
    indices = report_indices(membership, node_index)
    expected = HEADER_BYTES + REPORT_BYTES * len(indices)
    if len(data) != expected:
        raise BroadcastError("length", f"{len(data)} bytes; membership {membership:#010x} needs {expected}")
    reports = []
    for k, index in enumerate(indices):
        score_q, rssi, counts, flags = _REPORT.unpack_from(data, HEADER_BYTES + REPORT_BYTES * k)
        reports.append(LinkReport(index, score_q, None if rssi == RSSI_NONE else rssi, counts >> 4, counts & 0x0F,
                                  flags & FLAGS_KNOWN, flags >> LATENESS_SHIFT))
    return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                          _format_id(node_id), bytes(tag), tuple(reports))


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
    indices = ([] if broadcast.frame_type in JOIN_FRAMES
               else report_indices(broadcast.membership, broadcast.node_index))
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
    return bytes(out)


def quantise_score(score: Optional[float]) -> int:
    """A score as the link report's score code (``csi.score_code.encode_score``): 0 for no score, else 1..255."""
    return encode_score(score)


def dequantise_score(code: int) -> Optional[float]:
    """The score a score code stands for (``csi.score_code.decode_score``): None for 0, no score yet."""
    return decode_score(code)


def _format_id(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def _parse_id(node_id: str) -> bytes:
    raw = bytes.fromhex(node_id.replace(":", ""))
    if len(raw) != NODE_ID_BYTES:
        raise ValueError(f"node id must be {NODE_ID_BYTES} bytes, got {node_id!r}")
    return raw
