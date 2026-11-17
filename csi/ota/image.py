"""The node image the host serves: the same checks a node's signed-app verification makes, and the kit's
trusted signing key digest (docs/ota.md, ADR-0031).
"""
from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import ReasonError

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


class ImageError(ReasonError):
    """An image the nodes must not get. ``reason`` is one of IMAGE_REASONS."""


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


def load_signer(path) -> bytes:
    """The trusted signing key digest in ``path``: 32 raw bytes (espsecure digest-sbv2-public-key) or 64 hex digits.
    Raises FileNotFoundError, or ValueError for a file that holds neither."""
    raw = Path(path).read_bytes()
    if len(raw) == 32:
        return raw
    try:
        digest = bytes.fromhex(raw.decode().strip())
    except (UnicodeDecodeError, ValueError):
        digest = b""
    if len(digest) != 32:
        raise ValueError(f"{path} holds neither 32 bytes nor 64 hex digits")
    return digest
