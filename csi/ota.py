"""Over-the-air updates of the node image, on the host (docs/ota.md, docs/adr/0029).

    image = check_image(data, trusted_key_digest)   # refuse an unsigned, corrupt or foreign-signed image
    frame = maintenance_command(OTA_CMD_UPDATE, targets, kit_key, image=image, port=8070, ap_channel=36)
    ssid, passphrase = maintenance_network(kit_key)  # the access point the host provides
    report = parse_report(line, kit_key)             # what a node POSTs after maintenance mode

The node takes an update only if ESP-IDF's signed-app check passes (Secure Boot V2 signature block, RSA-3072,
verified against the key of the image the node runs). ``check_image`` does the same check on the host, so a bad
image is refused before any node leaves the round for it: the ESP image format, its appended SHA-256, the
signature block (magic, CRC, image digest), the RSA-PSS signature, and the signer's public key digest (what
``espsecure digest-sbv2-public-key`` writes) against the kit's trusted one.

The maintenance command is frame type 8 of the round broadcast format (docs/round-broadcast.md
"Maintenance command"), signed with the kit key; firmware/core/ota_core.c is the node's copy and both are pinned to
tests/fixtures/ota_command.json.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass
from typing import Optional

from .round_auth import kit_key_check, sign_broadcast

# ---------------------------------------------------------------- the image

CHIP_ID_ESP32C5 = 23           # esptool's IMAGE_CHIP_ID for the ESP32-C5
IMAGE_MAGIC = 0xE9
HEADER_BYTES = 24              # common header (8) + extended header (16)
SECTOR = 4096                  # the signature sector starts on a 4 KB boundary
SIG_BLOCK_BYTES = 1216
SIG_BLOCK_MAGIC = 0xE7
SIG_BLOCK_RSA = 0x02           # Secure Boot V2, RSA-3072 PSS
RSA_BYTES = 384
APP_DESC_MAGIC = 0xABCD5432
IMAGE_REASONS = ("format", "chip", "digest", "unsigned", "signature", "signer")


class ImageError(ValueError):
    """An image the nodes must not get. ``reason`` is one of IMAGE_REASONS."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ImageInfo:
    size: int              # bytes of the signed image file, as served
    sha256: bytes          # SHA-256 of the whole file: what the maintenance command names
    signer: bytes          # SHA-256 of the signing public key (espsecure digest-sbv2-public-key)
    chip_id: int
    version: Optional[str]  # the app's version string (esp_app_desc_t), None if the image has none


def _program_end(data: bytes) -> int:
    """Where the program part (segments, checksum, appended SHA-256) ends; raises ImageError('format')."""
    if len(data) < HEADER_BYTES or data[0] != IMAGE_MAGIC:
        raise ImageError("format", "not an ESP app image (no 0xE9 header)")
    segments = data[1]
    if not 1 <= segments <= 16:
        raise ImageError("format", f"an ESP app image has 1 to 16 segments, not {segments}")
    if data[23] != 1:
        raise ImageError("format", "the image has no appended SHA-256 (built without it?)")
    pos = HEADER_BYTES
    for _ in range(segments):
        if pos + 8 > len(data):
            raise ImageError("format", "the image is cut short in a segment header")
        length = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8 + length
        if pos > len(data):
            raise ImageError("format", "the image is cut short in a segment")
    pos += 15 - pos % 16          # pad so the checksum byte ends a 16-byte block
    end = pos + 1 + 32            # checksum, then SHA-256
    if end > len(data):
        raise ImageError("format", "the image is cut short before its SHA-256")
    return end


def _checksum_ok(data: bytes) -> bool:
    checksum, pos = 0xEF, HEADER_BYTES
    for _ in range(data[1]):
        length = struct.unpack_from("<I", data, pos + 4)[0]
        for b in data[pos + 8:pos + 8 + length]:
            checksum ^= b
        pos += 8 + length
    pos += 15 - pos % 16
    return data[pos] == checksum


def _mgf1(seed: bytes, length: int) -> bytes:
    out = b""
    for counter in range((length + 31) // 32):
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
    return out[:length]


def _rsa_pss_sha256_ok(n: int, e: int, signature: int, digest: bytes, salt_len: int = 32) -> bool:
    """RFC 8017 RSASSA-PSS-VERIFY (EMSA-PSS, SHA-256, MGF1-SHA-256) of a 32-byte digest."""
    mod_bits = n.bit_length()
    if signature >= n or mod_bits < 1024:
        return False
    em_bits = mod_bits - 1
    em_len = (em_bits + 7) // 8
    em = pow(signature, e, n).to_bytes((mod_bits + 7) // 8, "big")[-em_len:]
    h_len = 32
    if em[-1] != 0xBC or em_len < h_len + salt_len + 2:
        return False
    masked, h = em[:em_len - h_len - 1], em[em_len - h_len - 1:-1]
    top = 8 * em_len - em_bits    # leftmost bits of the encoded message that must be zero
    if top and masked[0] >> (8 - top):
        return False
    db = bytearray(a ^ b for a, b in zip(masked, _mgf1(h, len(masked))))
    db[0] &= 0xFF >> top
    pad = em_len - h_len - salt_len - 2
    if any(db[:pad]) or db[pad] != 0x01:
        return False
    salt = bytes(db[-salt_len:])
    return hmac.compare_digest(hashlib.sha256(bytes(8) + digest + salt).digest(), h)


def check_image(data: bytes, trusted_key_digest: bytes, chip_id: int = CHIP_ID_ESP32C5) -> ImageInfo:
    """The image's facts if a node would take it; else ImageError naming why not (IMAGE_REASONS)."""
    data = bytes(data)
    end = _program_end(data)
    chip = struct.unpack_from("<H", data, 12)[0]
    if chip != chip_id:
        raise ImageError("chip", f"the image is for chip id {chip}, not {chip_id} (ESP32-C5)")
    if hashlib.sha256(data[:end - 32]).digest() != data[end - 32:end] or not _checksum_ok(data):
        raise ImageError("digest", "the image does not match its own SHA-256 or checksum: it is corrupt")
    sig_at = -(-end // SECTOR) * SECTOR
    if len(data) < sig_at + SIG_BLOCK_BYTES or data[sig_at] != SIG_BLOCK_MAGIC:
        raise ImageError("unsigned", "the image is not signed: sign it with espsecure sign-data --version 2")
    block = data[sig_at:sig_at + SIG_BLOCK_BYTES]
    if block[1] != SIG_BLOCK_RSA:
        raise ImageError("signature", f"signature block version {block[1]} is not RSA (Secure Boot V2, 0x02)")
    if struct.unpack_from("<I", block, 1196)[0] != zlib.crc32(block[:1196]) & 0xFFFFFFFF:
        raise ImageError("signature", "the signature block's CRC does not match: it was changed")
    digest = hashlib.sha256(data[:sig_at]).digest()
    if block[4:36] != digest:
        raise ImageError("signature", "the image under the signature is not the one that was signed")
    n = int.from_bytes(block[36:36 + RSA_BYTES], "little")
    e = struct.unpack_from("<I", block, 36 + RSA_BYTES)[0]
    signature = int.from_bytes(block[36 + 776:36 + 776 + RSA_BYTES], "little")
    if not _rsa_pss_sha256_ok(n, e, signature, digest):
        raise ImageError("signature", "the RSA-PSS signature does not verify")
    signer = hashlib.sha256(block[36:36 + 776]).digest()
    if not trusted_key_digest or not hmac.compare_digest(signer, bytes(trusted_key_digest)):
        raise ImageError("signer", f"signed by key {signer.hex()[:16]}..., not the kit's signing key")
    return ImageInfo(size=len(data), sha256=hashlib.sha256(data).digest(), signer=signer, chip_id=chip,
                     version=_app_version(data))


def _app_version(data: bytes) -> Optional[str]:
    """esp_app_desc_t.version, at the start of the first segment's data in an ESP-IDF app."""
    at = HEADER_BYTES + 8
    if len(data) < at + 48 or struct.unpack_from("<I", data, at)[0] != APP_DESC_MAGIC:
        return None
    return data[at + 16:at + 48].split(b"\0", 1)[0].decode("utf-8", "replace")


# ---------------------------------------------------------------- the maintenance command

FRAME_MAINTENANCE = 8          # round broadcast frame type of a maintenance command
OTA_CMD_UPDATE = 1             # fetch, verify and boot the image the command names
OTA_CMD_REPORT = 2             # only report the running image
MAX_TARGETS = 13               # 26 + 46 + 6 x 13 = 150 bytes, the most a node signs or checks
HOST_ID = bytes(6)             # the header's node id on a command: the host
_BODY = struct.Struct("<BBHBBII32s")   # command, targets, port, ap channel, reserved, issued, image size, SHA-256
_COMMAND_HEAD = 26 + _BODY.size        # 72
COMMAND_REASONS = ("short", "version", "frame_type", "length", "targets", "command", "field")


class CommandError(ValueError):
    """Bytes that are not a maintenance command a node would take. ``reason`` is one of COMMAND_REASONS."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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


def channels_clear(sensing_channel: int, ap_channel: int) -> bool:
    """True when a maintenance network on ``ap_channel`` cannot overlap the 20 MHz sensing channel: 5 GHz
    (36..177), a 2.4 GHz channel at least 5 away, or 0 (the node scans and checks the channel it finds)."""
    if ap_channel == 0 or 36 <= ap_channel <= 177:
        return True
    return 1 <= ap_channel <= 14 and abs(ap_channel - sensing_channel) >= 5


def _serial_bytes(serial: str) -> bytes:
    try:
        raw = bytes.fromhex(serial.replace(":", ""))
    except ValueError:
        raw = b""
    if len(raw) != 6:
        raise ValueError(f"{serial!r} is not a board serial (6 bytes, e.g. 02:00:00:00:01:5C)")
    return raw


def _serial_text(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


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
    header = struct.pack("<BBHBBBBI6s8s", 2, FRAME_MAINTENANCE, round_no & 0xFFFF, 0, 0, 0, 0, 0, HOST_ID, bytes(8))
    body = _BODY.pack(command, len(targets), port, ap_channel, 0, issued & 0xFFFFFFFF, image_size,
                      bytes(image_sha256))
    return sign_broadcast(header + body + b"".join(_serial_bytes(t) for t in targets), kit_key)


def decode_command(data: bytes) -> Command:
    """The node's decoder (firmware/core/ota_core.c ota_command_decode) on the host. The tag is not checked."""
    data = bytes(data)
    if len(data) < 26:
        raise CommandError("short", "shorter than the round header")
    if data[0] != 2:
        raise CommandError("version", f"format version {data[0]}, not 2")
    if data[1] != FRAME_MAINTENANCE:
        raise CommandError("frame_type", f"frame type {data[1]} is not a maintenance command")
    if len(data) < _COMMAND_HEAD:
        raise CommandError("length", "cut short before the targets")
    command, count, port, channel, reserved, issued, size, sha = _BODY.unpack_from(data, 26)
    if not 1 <= count <= MAX_TARGETS:
        raise CommandError("targets", f"{count} targets (1 to {MAX_TARGETS})")
    if len(data) != _COMMAND_HEAD + 6 * count:
        raise CommandError("length", f"{len(data)} bytes for {count} targets")
    if command not in (OTA_CMD_UPDATE, OTA_CMD_REPORT):
        raise CommandError("command", f"unknown command {command}")
    if port == 0 or (command == OTA_CMD_UPDATE and size == 0) or reserved or any(data[4:12]):
        raise CommandError("field", "port 0, an update without a size, or a reserved field set")
    targets = tuple(_serial_text(data[_COMMAND_HEAD + 6 * i:_COMMAND_HEAD + 6 * i + 6]) for i in range(count))
    return Command(command, targets, port, channel, issued, size, sha, struct.unpack_from("<H", data, 2)[0])


# ---------------------------------------------------------------- the maintenance network

NETWORK_LABEL = b"shadowcast maintenance network"
SSID_PREFIX = "shadowcast-"


def maintenance_network(kit_key: bytes) -> tuple[str, str]:
    """(SSID, WPA2 passphrase) of the access point the host provides for maintenance mode. Both follow from the
    kit key (the node derives the same), so no node needs a second secret."""
    passphrase = hmac.new(kit_key, NETWORK_LABEL, hashlib.sha256).digest()[:12].hex()
    return SSID_PREFIX + kit_key_check(kit_key), passphrase


# ---------------------------------------------------------------- the node's report

REPORT_TAG_LABEL = b"shadowcast ota report\n"
# firmware/core/ota_core.h ota_outcome_t, in order
OUTCOMES = ("none", "committed", "rolled_back", "already", "checked_in", "no_network", "overlap", "no_server",
            "refused_size", "refused_digest", "refused_image", "write_failed")
GOOD_OUTCOMES = frozenset({"committed", "already", "checked_in"})
_REPORT_FIELDS = ("serial", "issued", "outcome", "slot", "image", "version")


class ReportError(ValueError):
    """A report line that is malformed or does not carry the kit's tag."""


@dataclass(frozen=True)
class Report:
    serial: str
    issued: int        # the command it answers
    outcome: str       # one of OUTCOMES
    slot: str          # the app slot the node runs now, ota_0 or ota_1
    image: str         # first 8 bytes of the running image's SHA-256 as hex, "-" when the node does not know it
    version: str       # the running image's version string

    @property
    def ok(self) -> bool:
        """The node runs the commanded image (or was only asked to report)."""
        return self.outcome in GOOD_OUTCOMES


def report_tag(line: str, kit_key: bytes) -> str:
    return hmac.new(kit_key, REPORT_TAG_LABEL + line.encode(), hashlib.sha256).digest()[:8].hex()


def parse_report(line: str, kit_key: bytes) -> Report:
    """The report a node POSTs: "ota-report 1 serial=.. issued=.. outcome=.. slot=.. image=.. version=.. tag=..",
    whose tag is HMAC-SHA256(kit key, REPORT_TAG_LABEL + the line before " tag=")[:8]."""
    line = line.strip()
    body, sep, tag = line.rpartition(" tag=")
    if not sep or not hmac.compare_digest(report_tag(body, kit_key), tag):
        raise ReportError("the report does not carry the kit's tag")
    words = body.split(" ")
    if words[:2] != ["ota-report", "1"]:
        raise ReportError(f"not a version 1 report: {body[:40]!r}")
    fields = dict(w.split("=", 1) for w in words[2:] if "=" in w)
    if any(k not in fields for k in _REPORT_FIELDS):
        raise ReportError(f"the report lacks a field: {body!r}")
    try:
        issued = int(fields["issued"])
    except ValueError:
        raise ReportError(f"issued is not a number: {fields['issued']!r}") from None
    return Report(fields["serial"].upper(), issued, fields["outcome"], fields["slot"], fields["image"],
                  fields["version"])
