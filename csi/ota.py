"""Over-the-air updates of the node image, on the host (docs/ota.md, docs/adr/0031).

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
import time
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


# ---------------------------------------------------------------- the staged rollout

STAGE_SIZES = (2, 6)           # the first stages; then the rest, MAX_TARGETS per command
COPIES = 3                     # each command is sent this often (a broadcast has no ACK) ...
COPY_GAP_S = 0.2               # ... this far apart
REPORT_TIMEOUT_S = 300.0       # a target's report must arrive this long after its stage's command
REJOIN_TIMEOUT_S = 60.0        # and it must be heard on the round again this long after its report


def plan_stages(serials, sizes=STAGE_SIZES, per_command: int = MAX_TARGETS) -> list[list[str]]:
    """The nodes in stages: ``sizes`` first (2, then 6), then the rest, at most ``per_command`` per stage."""
    rest, stages = list(serials), []
    for size in sizes:
        if rest:
            stages.append(rest[:min(size, per_command)])
            rest = rest[len(stages[-1]):]
    while rest:
        stages.append(rest[:per_command])
        rest = rest[per_command:]
    return stages


class Rollout:
    """One command at a time, stage after stage, each stage only once the last one is fully back.

    The caller polls ``step(now)`` and feeds what it learns: ``report(r, now)`` for every report the image server
    receives, ``heard(serial, now)`` for every authenticated round broadcast the gateway hears. A stage passes when
    every target reported a good outcome (GOOD_OUTCOMES) for this command and was heard on the round after that.
    Anything else (a bad outcome, no report within ``report_timeout_s``, not back on the round within
    ``rejoin_timeout_s``) stops the rollout: ``state`` "failed", ``failure`` says why, and no further stage is sent.
    ``send(frame)`` hands one signed maintenance command to the gateway.
    """

    def __init__(self, stages, kit_key: bytes, *, image: Optional[ImageInfo], port: int, ap_channel: int,
                 sensing_channel: int, send, command: int = OTA_CMD_UPDATE, wall=None, log=print,
                 report_timeout_s: float = REPORT_TIMEOUT_S, rejoin_timeout_s: float = REJOIN_TIMEOUT_S,
                 copies: int = COPIES, copy_gap_s: float = COPY_GAP_S) -> None:
        if not channels_clear(sensing_channel, ap_channel):
            raise ValueError(f"a maintenance network on channel {ap_channel} overlaps sensing channel "
                             f"{sensing_channel}: put it on 5 GHz or 5+ channels away")
        if command == OTA_CMD_UPDATE and image is None:
            raise ValueError("an update needs a checked image")
        self._stages = [list(s) for s in stages if s]
        self._key = bytes(kit_key)
        self._image, self._command = image, command
        self._port, self._ap_channel, self._sensing = port, ap_channel, sensing_channel
        self._send, self._log = send, log
        self._wall = wall or time.time
        self._report_timeout, self._rejoin_timeout = report_timeout_s, rejoin_timeout_s
        self._copies, self._gap = copies, copy_gap_s
        self._stage = -1
        self._issued = 0
        self.state = "running" if self._stages else "done"
        self.failure = ""
        self.results: dict[str, str] = {}

    # -- the caller's events

    def report(self, r: Report, now: float) -> None:
        serial = r.serial.upper()
        if self.state != "running" or self._stage < 0 or r.issued != self._issued or serial not in self._targets:
            return
        self.results[serial] = r.outcome
        self._reported.setdefault(serial, now)
        self._log(f"{serial}: {r.outcome} (slot {r.slot}, version {r.version})")
        if not r.ok:
            self._fail(f"{serial} reported {r.outcome}")

    def heard(self, serial: str, now: float) -> None:
        serial = serial.upper()
        if self.state == "running" and self._stage >= 0 and serial in self._reported and now >= self._reported[serial]:
            self._rejoined.add(serial)

    # -- the clock

    def step(self, now: float) -> str:
        if self.state != "running":
            return self.state
        if self._stage < 0 or self._stage_done():
            if self._stage + 1 >= len(self._stages):
                self.state = "done"
                self._log("rollout done")
                return self.state
            self._start(self._stage + 1, now)
        while self._copies_left and now >= self._next_copy:
            self._send(self._frame)
            self._copies_left -= 1
            self._next_copy += self._gap
        self._check_timeouts(now)
        return self.state

    # -- inside

    def _start(self, stage: int, now: float) -> None:
        self._stage = stage
        self._targets = [s.upper() for s in self._stages[stage]]
        self._issued = max(int(self._wall()), self._issued + 1)
        image = self._image
        self._frame = maintenance_command(
            self._command, self._targets, self._key, port=self._port, ap_channel=self._ap_channel,
            issued=self._issued, image_size=image.size if image else 0,
            image_sha256=image.sha256 if image else bytes(32), round_no=self._issued,
            sensing_channel=self._sensing)
        self._copies_left, self._next_copy, self._started = self._copies, now, now
        self._reported: dict[str, float] = {}
        self._rejoined: set[str] = set()
        self._log(f"stage {stage + 1}/{len(self._stages)}: {', '.join(self._targets)}")

    def _stage_done(self) -> bool:
        return all(self.results.get(s) in GOOD_OUTCOMES and s in self._rejoined for s in self._targets)

    def _check_timeouts(self, now: float) -> None:
        for s in self._targets:
            if s not in self._reported and now - self._started >= self._report_timeout:
                return self._fail(f"no report from {s} within {self._report_timeout:.0f} s")
            if s in self._reported and s not in self._rejoined and now - self._reported[s] >= self._rejoin_timeout:
                return self._fail(f"{s} was not heard on the round within {self._rejoin_timeout:.0f} s of its report")

    def _fail(self, why: str) -> None:
        self.state, self.failure = "failed", why
        self._log(f"rollout stopped: {why}")


# ---------------------------------------------------------------- the image server

MAX_REPORT_BYTES = 512


class ImageServer:
    """The HTTP server nodes pull from in maintenance mode, on the maintenance network.

        GET  /node.bin?serial=S   the signed image (``fetches`` lists the serials that asked)
        POST /report              one report line with the kit's tag; 200, or 400 when it does not verify

    It serves one public, signed image and takes tagged reports, nothing else; ``host`` is the maintenance
    network's address (default every interface, since the nodes come from the access point's subnet).
    """

    def __init__(self, image: bytes, kit_key: bytes, host: str = "0.0.0.0", port: int = 8070, log=None) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        self._image, self._key = bytes(image), bytes(kit_key)
        self._lock = threading.Lock()
        self._reports: list[Report] = []
        self.fetches: list[str] = []
        self.refused_reports = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (the stdlib's name)
                parts = urlsplit(self.path)
                if parts.path != "/node.bin":
                    return self._reply(404, b"not found\n")
                serial = parse_qs(parts.query).get("serial", ["?"])[0].upper()
                with outer._lock:
                    outer.fetches.append(serial)
                if log:
                    log(f"{serial}: fetching the image")
                self._reply(200, outer._image, "application/octet-stream")

            def do_POST(self):  # noqa: N802
                if urlsplit(self.path).path != "/report":
                    return self._reply(404, b"not found\n")
                length = min(int(self.headers.get("Content-Length") or 0), MAX_REPORT_BYTES)
                text = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    report = parse_report(text, outer._key)
                except ReportError:
                    with outer._lock:
                        outer.refused_reports += 1
                    return self._reply(400, b"refused\n")
                with outer._lock:
                    outer._reports.append(report)
                self._reply(200, b"ok\n")

            def _reply(self, code, body, kind="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):   # quiet: the rollout logs what matters
                pass

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ota-image-server", daemon=True)

    def start(self) -> "ImageServer":
        self._thread.start()
        return self

    def take_reports(self) -> list[Report]:
        """The reports received since the last call."""
        with self._lock:
            reports, self._reports = self._reports, []
        return reports

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
