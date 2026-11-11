"""Authenticated round broadcasts (format v2, docs/round-broadcast.md "Authentication") against the golden fixtures.

The tags in tests/fixtures/round_broadcast_v2.json were computed with openssl, not by csi/round_auth.py, and the
firmware's C core replays the same cases (firmware/core/tests/test_round_auth.c).
"""
import json
from pathlib import Path

import pytest

from csi.round_auth import (FRESH, REPLAY, TEST_KIT_KEY, Freshness, KitKeyError, kit_key_check, load_kit_key,
                            sign_broadcast, tag_ok, write_kit_key)

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "round_broadcast_v2.json").read_text())
KEY = bytes.fromhex(GOLDEN["key"])
VALID = {case["name"]: case for case in GOLDEN["valid"]}
FORGED = {case["name"]: case for case in GOLDEN["forged"]}


def unsigned(data: bytes) -> bytes:
    return data[:18] + bytes(8) + data[26:]


def test_the_fixture_key_is_the_published_test_key():
    assert KEY == TEST_KIT_KEY


@pytest.mark.parametrize("name", sorted(VALID))
def test_signing_writes_the_golden_tag(name):
    golden = bytes.fromhex(VALID[name]["hex"])
    assert sign_broadcast(unsigned(golden), KEY) == golden


@pytest.mark.parametrize("name", sorted(VALID))
def test_golden_broadcasts_verify(name):
    assert tag_ok(bytes.fromhex(VALID[name]["hex"]), KEY)


@pytest.mark.parametrize("name", sorted(FORGED))
def test_forged_broadcasts_do_not_verify(name):
    assert not tag_ok(bytes.fromhex(FORGED[name]["hex"]), KEY)


def test_signing_ignores_whatever_was_in_the_tag_field():
    golden = bytes.fromhex(VALID["lone_node_no_reports"]["hex"])
    assert sign_broadcast(golden[:18] + b"\xff" * 8 + golden[26:], KEY) == golden


def test_a_broadcast_signed_with_another_key_does_not_verify():
    golden = bytes.fromhex(VALID["three_nodes_middle_slot"]["hex"])
    other = bytes(reversed(KEY))
    assert tag_ok(sign_broadcast(golden, other), other)
    assert not tag_ok(sign_broadcast(golden, other), KEY)


def test_bytes_shorter_than_the_header_never_verify():
    assert not tag_ok(bytes.fromhex(VALID["lone_node_no_reports"]["hex"])[:20], KEY)


def test_the_key_check_value_names_a_key_without_revealing_it():
    assert kit_key_check(KEY) == GOLDEN["key_check"]
    assert kit_key_check(bytes(16)) != GOLDEN["key_check"]


def test_the_freshness_rule_follows_the_golden_sequence():
    fresh = Freshness()
    got = [fresh.check(step["node_id"], step["round"], step["t_us"] / 1e6) for step in GOLDEN["freshness"]]
    assert got == [FRESH if step["expect"] == "fresh" else REPLAY for step in GOLDEN["freshness"]]


def test_freshness_matches_node_ids_in_any_case():
    fresh = Freshness()
    assert fresh.check("aa:bb:cc:dd:ee:ff", 10, 0.0) == FRESH
    assert fresh.check("AA:BB:CC:DD:EE:FF", 10, 0.025) == REPLAY


def test_a_kit_key_file_round_trips_and_is_not_overwritten(tmp_path):
    path = tmp_path / "kit.key"
    key = write_kit_key(path)
    assert len(key) == 16 and key != TEST_KIT_KEY
    assert load_kit_key(path) == key
    with pytest.raises(KitKeyError, match="exists"):
        write_kit_key(path)


@pytest.mark.parametrize("text, why", [
    ("", "32 hex digits"),
    ("00112233", "32 hex digits"),
    ("zz" * 16, "32 hex digits"),
    (TEST_KIT_KEY.hex(), "test key"),
])
def test_a_bad_kit_key_file_is_refused(tmp_path, text, why):
    path = tmp_path / "kit.key"
    path.write_text(text + "\n")
    with pytest.raises(KitKeyError, match=why):
        load_kit_key(path)


def test_a_missing_kit_key_file_says_how_to_make_one(tmp_path):
    with pytest.raises(KitKeyError, match="keygen"):
        load_kit_key(tmp_path / "nope.key")
