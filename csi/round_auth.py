"""Authenticated round broadcasts (format v2): the tag, the freshness rule and the kit key, on the host.

docs/round-broadcast.md "Authentication" is the definition; the golden cases are in
tests/fixtures/round_broadcast_v2.json, and firmware/core/round_auth.h is the node's copy.

    data = sign_broadcast(encode_broadcast(b), key)   # write the tag
    tag_ok(data, key)                                  # False: forged, or another kit's key
    fresh = Freshness(); fresh.check(node_id, round, host_ts)   # FRESH, or STALE for a stale round number

The tag is the first 8 bytes of HMAC-SHA256(kit key, the broadcast with its tag bytes 18..25 set to zero). It
covers every other byte, the node id and the round number included. The kit key is 16 random bytes shared by every
node of a kit and the host; it gives authenticity, not secrecy (nothing is encrypted).

The round number guards against a recorded copy sent again (a replay attack). Per sender, a broadcast is fresh when its round number is ahead of the
last fresh one from that node, by at most ``AHEAD_SLACK`` + 1 round per millisecond since then (no rig runs a
round faster than 1 ms per round). Anything else is stale, unless the node has been silent (no fresh broadcast)
for more than ``RESYNC_S``: then any round number is taken, because a node that restarted or renumbered its rounds
starts again from wherever it is. The limits are in docs/round-broadcast.md.

``TEST_KIT_KEY`` is the published key of the fixtures and the simulator. ``load_kit_key`` refuses it, so a real kit
never runs on it.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
from pathlib import Path
from typing import Union

from .broadcast import HEADER_BYTES, TAG_BYTES, TAG_OFFSET

KIT_KEY_BYTES = 16
TEST_KIT_KEY = bytes(range(KIT_KEY_BYTES))   # 000102..0f: fixtures and simulator only, never a real kit
DEFAULT_KIT_KEY_FILE = "data/kit.key"         # data/ is not in git
KEY_CHECK_MESSAGE = b"shadowcast kit key check"
AHEAD_SLACK = 16          # rounds a fresh broadcast may be ahead of the last one, plus 1 per millisecond since
MAX_ROUNDS_PER_S = 1000   # that 1 per millisecond: no rig runs a round shorter than 1 ms
RESYNC_S = 2.0            # a node silent this long is taken at whatever round number it sends next
FRESH = "fresh"
STALE = "stale"         # rejection reason: the tag verifies but the round number is stale
_ZERO_TAG = bytes(TAG_BYTES)


class KitKeyError(ValueError):
    """A kit key file that is missing, malformed or holds the published test key."""


def broadcast_tag(data: bytes, key: bytes) -> bytes:
    """The authentication tag of round broadcast ``data`` (whatever its tag bytes hold) under ``key``."""
    message = bytes(data[:TAG_OFFSET]) + _ZERO_TAG + bytes(data[TAG_OFFSET + TAG_BYTES:])
    return hmac.new(key, message, hashlib.sha256).digest()[:TAG_BYTES]


def sign_broadcast(data: bytes, key: bytes) -> bytes:
    """``data`` with its tag written."""
    return bytes(data[:TAG_OFFSET]) + broadcast_tag(data, key) + bytes(data[TAG_OFFSET + TAG_BYTES:])


def tag_ok(data: bytes, key: bytes) -> bool:
    """True if ``data`` carries the tag ``key`` gives it. Bytes shorter than the round header never verify."""
    if len(data) < HEADER_BYTES:
        return False
    return hmac.compare_digest(bytes(data[TAG_OFFSET:TAG_OFFSET + TAG_BYTES]), broadcast_tag(data, key))


def kit_key_check(key: bytes) -> str:
    """The key's check value: 8 hex digits that tell two keys apart without revealing either. A node shows the
    same value for its key (scripts/node.py PORT key)."""
    return hmac.new(key, KEY_CHECK_MESSAGE, hashlib.sha256).digest()[:4].hex()


class Freshness:
    """The freshness rule: the last fresh round number of every sender, by node id."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, float]] = {}   # node id (upper case) -> (round, time of that broadcast)

    def check(self, node_id: str, round_no: int, t: float) -> str:
        """FRESH (and remembered) or STALE for an authenticated broadcast from ``node_id`` heard at ``t`` seconds."""
        node = node_id.upper()
        last = self._last.get(node)
        if last is not None and t - last[1] <= RESYNC_S:
            ahead = (round_no - last[0] + 32768) % 65536 - 32768
            if not 1 <= ahead <= AHEAD_SLACK + math.floor(max(0.0, t - last[1]) * MAX_ROUNDS_PER_S):
                return STALE
        self._last[node] = (round_no, t)
        return FRESH

    def __len__(self) -> int:
        return len(self._last)


def load_kit_key(path: Union[str, Path]) -> bytes:
    """The kit key in ``path``: one line of 32 hex digits; lines starting with # are comments."""
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise KitKeyError(f"no kit key at {path}: make one with `python scripts/node.py keygen {path}` and "
                          f"provision every node with it (docs/round-broadcast.md \"Kit key\")") from None
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    try:
        key = bytes.fromhex(lines[0]) if len(lines) == 1 else b""
    except ValueError:
        key = b""
    if len(key) != KIT_KEY_BYTES:
        raise KitKeyError(f"{path} must hold one line of {2 * KIT_KEY_BYTES} hex digits (the kit key)")
    if key == TEST_KIT_KEY:
        raise KitKeyError(f"{path} holds the published test key; make a real one with scripts/node.py keygen")
    return key


def write_kit_key(path: Union[str, Path]) -> bytes:
    """Make a new random kit key and write it to ``path``, which must not exist yet. Returns the key."""
    path = Path(path)
    key = os.urandom(KIT_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", newline="\n") as f:
            f.write(f"# shadowcast kit key, check {kit_key_check(key)}. Keep it off git and out of recordings.\n"
                    f"{key.hex()}\n")
    except FileExistsError:
        raise KitKeyError(f"{path} exists; a kit has one key, so it is not replaced") from None
    return key
