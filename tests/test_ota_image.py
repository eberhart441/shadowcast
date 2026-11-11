"""The host's check of a node image before it serves it over the air (csi/ota.py, docs/ota.md).

Pinned to a tiny ESP32-C5 image made by esptool 5.6.0 and signed by espsecure 5.6.0 (tests/fixtures/ota/): the
image ESP-IDF itself would take must pass, and an unsigned, corrupt or foreign-signed one must be refused with a
reason, exactly as the node's own check (ESP-IDF's signed-app verification) would refuse it.
"""
import json
import struct
import zlib
from pathlib import Path

import pytest

from csi import ota

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ota"
INFO = json.loads((FIXTURES / "image.json").read_text())
SIGNED = (FIXTURES / "signed.bin").read_bytes()
OTHER = (FIXTURES / "signed_by_other_key.bin").read_bytes()
TRUSTED = bytes.fromhex(INFO["test_key_digest"])


def refused(data, trusted=TRUSTED):
    with pytest.raises(ota.ImageError) as e:
        ota.check_image(data, trusted)
    return e.value.reason


def flip(data, offset):
    return data[:offset] + bytes([data[offset] ^ 0x01]) + data[offset + 1:]


def test_an_image_signed_by_the_trusted_key_passes():
    image = ota.check_image(SIGNED, TRUSTED)
    assert image.size == INFO["signed_length"]
    assert image.sha256.hex() == INFO["signed_sha256"]
    assert image.signer.hex() == INFO["test_key_digest"]
    assert image.chip_id == INFO["chip_id"]


def test_an_unsigned_image_is_refused():
    assert refused(SIGNED[:INFO["unsigned_length"]]) == "unsigned"


def test_a_signed_image_from_another_key_is_refused():
    assert refused(OTHER) == "signer"
    assert ota.check_image(OTHER, bytes.fromhex(INFO["other_key_digest"])).signer.hex() == INFO["other_key_digest"]


@pytest.mark.parametrize("offset", [40, 1000, INFO["unsigned_length"] - 40])
def test_a_corrupt_program_is_refused(offset):
    assert refused(flip(SIGNED, offset)) == "digest"


def test_a_change_after_the_program_but_under_the_signature_is_refused():
    assert refused(flip(SIGNED, INFO["unsigned_length"] + 100)) == "signature"   # the 0xff padding


@pytest.mark.parametrize("where", [4 + 10, 36 + 20, 36 + 776 + 50])   # image digest, public key, signature
def test_a_changed_signature_block_is_refused(where):
    assert refused(flip(SIGNED, len(SIGNED) - 4096 + where)) == "signature"


def with_block_crc_fixed(data):
    """The CRC is no protection: anyone can recompute it after changing the block."""
    at = len(data) - 4096
    block = data[at:at + 1196]
    return data[:at + 1196] + struct.pack("<I", zlib.crc32(block) & 0xFFFFFFFF) + data[at + 1200:]


def test_a_changed_signature_with_a_fixed_crc_is_refused():
    assert refused(with_block_crc_fixed(flip(SIGNED, len(SIGNED) - 4096 + 36 + 776 + 50))) == "signature"


def test_another_keys_signature_on_this_image_is_refused():
    """Swap in the other key's public key but keep this signature: the RSA check fails, not the signer check."""
    at = len(SIGNED) - 4096
    mixed = SIGNED[:at + 36] + OTHER[at + 36:at + 36 + 776] + SIGNED[at + 36 + 776:]
    assert refused(with_block_crc_fixed(mixed), bytes.fromhex(INFO["other_key_digest"])) == "signature"


def test_an_image_for_another_chip_is_refused():
    assert refused(SIGNED[:12] + bytes([5, 0]) + SIGNED[14:]) == "chip"   # chip id 5: ESP32-C3


@pytest.mark.parametrize("data", [b"", b"\xe9", b"not an image at all" * 100, SIGNED[:300]])
def test_something_that_is_not_an_image_is_refused(data):
    assert refused(data) == "format"


def test_no_trusted_key_is_refused_before_anything_is_served():
    with pytest.raises(ota.ImageError) as e:
        ota.check_image(SIGNED, b"")
    assert e.value.reason == "signer"
