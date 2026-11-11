"""Round broadcast format v1: encode and decode the bytes a transceiver sends once per round.

The format is defined in docs/round-broadcast.md; this module is the host's copy of it, pinned to the golden
byte fixtures in tests/fixtures/round_broadcast_v1.json, which the firmware must reproduce too. Change the
three together and bump ``FORMAT_VERSION`` when the layout changes.

    broadcast = decode_broadcast(data)     # RoundBroadcast, or BroadcastError with a .reason
    data = encode_broadcast(broadcast)

A round broadcast is a 26-byte round header and one 4-byte link report per other member of the round, in
ascending node index. Little-endian throughout.

Score quantisation (``quantise_score`` / ``dequantise_score``) is PROVISIONAL: the C scorer ticket owns the
u8 scale and replaces these two functions; nothing else in the host depends on the scale.
"""
from __future__ import annotations

import math
import struct
from typing import NamedTuple, Optional

FORMAT_VERSION = 1
FRAME_ROUND = 1                  # the only frame type in v1: a round broadcast carrying link reports
FRAME_TYPES = frozenset({FRAME_ROUND})
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

# link report flags (byte 3); bits 3-7 are reserved, sent as 0 and ignored on decode
FLAG_WARMING = 0x01              # the receiving node's per-link score is still warming up
FLAG_POSSIBLE_MOVE = 0x02        # the receiving node suspects a node moved (its baseline shifted)
FLAG_RAW_MODE = 0x04             # raw mode is active on this link
FLAGS_KNOWN = FLAG_WARMING | FLAG_POSSIBLE_MOVE | FLAG_RAW_MODE

# PROVISIONAL score scale (see the module doc): u8 0 is "no score yet", 1..255 are log-spaced from SCORE_MIN to
# SCORE_MAX, a step of about 7.5 %, so a dequantised score is within about 4 % of the score.
SCORE_MIN = 1e-4
SCORE_MAX = 1e4
_SCORE_STEPS = 254
_LOG_MIN = math.log10(SCORE_MIN)
_LOG_STEP = (math.log10(SCORE_MAX) - _LOG_MIN) / _SCORE_STEPS


class BroadcastError(ValueError):
    """Bytes that are not a v1 round broadcast. ``reason`` names why, for the rejection counters:
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
    tag: bytes                   # 8-byte authentication tag; zeros until authentication is defined
    reports: tuple[LinkReport, ...]


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
                                  flags & FLAGS_KNOWN))
    return RoundBroadcast(version, frame_type, round_no, slot, slots, schedule, node_index, membership,
                          _format_id(node_id), bytes(tag), tuple(reports))


def encode_broadcast(broadcast: RoundBroadcast) -> bytes:
    """The bytes of ``broadcast``. Its reports must follow the membership (``report_indices``)."""
    indices = report_indices(broadcast.membership, broadcast.node_index)
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
        out += _REPORT.pack(r.score_q, RSSI_NONE if r.rssi is None else r.rssi, r.heard << 4 | r.since, r.flags)
    return bytes(out)


def quantise_score(score: Optional[float]) -> int:
    """PROVISIONAL. A score as the link report's u8: 0 for no score, else 1..255 on a log scale (saturating)."""
    if score is None:
        return 0
    if not score > SCORE_MIN:
        return 1
    return int(min(_SCORE_STEPS, max(0, round((math.log10(score) - _LOG_MIN) / _LOG_STEP)))) + 1


def dequantise_score(q: int) -> Optional[float]:
    """PROVISIONAL. The score a u8 stands for (None for 0, no score yet)."""
    if q == 0:
        return None
    return 10 ** (_LOG_MIN + (q - 1) * _LOG_STEP)


def _format_id(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def _parse_id(node_id: str) -> bytes:
    raw = bytes.fromhex(node_id.replace(":", ""))
    if len(raw) != NODE_ID_BYTES:
        raise ValueError(f"node id must be {NODE_ID_BYTES} bytes, got {node_id!r}")
    return raw
