"""The maintenance command: frame type 8 of the round broadcast format (docs/round-broadcast.md "Maintenance
command"), signed with the kit key. firmware/core/ota_core.c is the node's copy; both are pinned to
tests/fixtures/ota_command.json.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from ..broadcast import HEADER_BYTES as ROUND_HEADER_BYTES
from ..broadcast import FORMAT_VERSION, FRAME_MAINTENANCE, encode_header, format_id, parse_id
from ..errors import ReasonError
from ..round_auth import sign_broadcast

OTA_CMD_UPDATE = 1             # fetch, verify and boot the image the command names
OTA_CMD_REPORT = 2             # only report the running image
MAX_TARGETS = 13               # 26 + 46 + 6 x 13 = 150 bytes, the most a node signs or checks
HOST_ID = "00:00:00:00:00:00"  # the header's node id on a command: the host
_BODY = struct.Struct("<BBHBBII32s")   # command, targets, port, ap channel, reserved, issued, image size, SHA-256
_COMMAND_HEAD = ROUND_HEADER_BYTES + _BODY.size   # 72
COMMAND_REASONS = ("short", "version", "frame_type", "length", "targets", "command", "field")


class CommandError(ReasonError):
    """Bytes that are not a maintenance command a node would take. ``reason`` is one of COMMAND_REASONS."""


@dataclass(frozen=True)
class Command:
    command: int
    targets: tuple[str, ...]   # board serials, "02:00:00:00:01:5C"
    port: int
    ap_channel: int
    issued: int
    image_size: int
    image_sha256: bytes
    round: int


def _block_80mhz(channel: int) -> int:
    """The 80 MHz block a 5 GHz channel lies in (36-48, 52-64, 100-112, ...; 149-161, 165-177)."""
    return 100 + (channel - 149) // 16 if channel >= 149 else (channel - 36) // 16


def channels_clear(sensing_channel: int, ap_channel: int) -> bool:
    """True when a maintenance network on ``ap_channel`` cannot overlap the 20 MHz sensing channel, or 0 (the node
    scans and checks the channel it finds). Sensing on 2.4 GHz: 5 GHz (36..177) or a 2.4 GHz channel at least 5
    away. Sensing on 5 GHz: 2.4 GHz, or a 5 GHz channel in another 80 MHz block (an access point may use all of
    one). firmware/core/ota_core.c ota_channels_clear is the node's copy."""
    ap_5ghz = 36 <= ap_channel <= 177
    if ap_channel == 0:
        return True
    if sensing_channel >= 36:
        return 1 <= ap_channel <= 14 or (ap_5ghz and _block_80mhz(ap_channel) != _block_80mhz(sensing_channel))
    if ap_5ghz:
        return True
    return 1 <= ap_channel <= 14 and abs(ap_channel - sensing_channel) >= 5


def maintenance_command(command: int, targets, kit_key: bytes, *, port: int, ap_channel: int, issued: int,
                        image_size: int = 0, image_sha256: bytes = bytes(32), round_no: int = 0,
                        sensing_channel: Optional[int] = None) -> bytes:
    """The signed bytes of one maintenance command for ``targets`` (board serials). Raises ValueError for a
    command a node would refuse or that would put the maintenance network on the sensing channel."""
    targets = list(targets)
    if not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError(f"a maintenance command names 1 to {MAX_TARGETS} nodes, not {len(targets)}")
    if command not in (OTA_CMD_UPDATE, OTA_CMD_REPORT):
        raise ValueError(f"unknown maintenance command {command}")
    if not 0 < port < 65536 or (command == OTA_CMD_UPDATE and image_size <= 0) or len(image_sha256) != 32:
        raise ValueError("a maintenance command needs a server port, and an update needs the image's size and SHA-256")
    if sensing_channel is not None and not channels_clear(sensing_channel, ap_channel):
        raise ValueError(f"a maintenance network on channel {ap_channel} overlaps sensing channel {sensing_channel}")
    header = encode_header(FRAME_MAINTENANCE, round_no, HOST_ID)
    body = _BODY.pack(command, len(targets), port, ap_channel, 0, issued & 0xFFFFFFFF, image_size,
                      bytes(image_sha256))
    return sign_broadcast(header + body + b"".join(parse_id(t) for t in targets), kit_key)


def decode_command(data: bytes) -> Command:
    """The node's decoder (firmware/core/ota_core.c ota_command_decode) on the host. The tag is not checked."""
    data = bytes(data)
    if len(data) < ROUND_HEADER_BYTES:
        raise CommandError("short", "shorter than the round header")
    if data[0] != FORMAT_VERSION:
        raise CommandError("version", f"format version {data[0]}, not {FORMAT_VERSION}")
    if data[1] != FRAME_MAINTENANCE:
        raise CommandError("frame_type", f"frame type {data[1]} is not a maintenance command")
    if len(data) < _COMMAND_HEAD:
        raise CommandError("length", "cut short before the targets")
    command, count, port, channel, reserved, issued, size, sha = _BODY.unpack_from(data, ROUND_HEADER_BYTES)
    if not 1 <= count <= MAX_TARGETS:
        raise CommandError("targets", f"{count} targets (1 to {MAX_TARGETS})")
    if len(data) != _COMMAND_HEAD + 6 * count:
        raise CommandError("length", f"{len(data)} bytes for {count} targets")
    if command not in (OTA_CMD_UPDATE, OTA_CMD_REPORT):
        raise CommandError("command", f"unknown command {command}")
    if port == 0 or (command == OTA_CMD_UPDATE and size == 0) or reserved or any(data[4:12]):
        raise CommandError("field", "port 0, an update without a size, or a reserved field set")
    targets = tuple(format_id(data[_COMMAND_HEAD + 6 * i:_COMMAND_HEAD + 6 * i + 6]) for i in range(count))
    return Command(command, targets, port, channel, issued, size, sha, struct.unpack_from("<H", data, 2)[0])
