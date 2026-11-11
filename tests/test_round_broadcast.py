"""Round broadcast format v2: the encoder and decoder against the golden byte fixtures (docs/round-broadcast.md).

The fixtures' layout bytes are written by hand and their tags computed with openssl, so they are the independent
source of truth that the firmware's encoder and decoder must also reproduce. v2 is v1's layout with a real
authentication tag (tests/test_round_auth.py); the frozen v1 fixtures pin that the layout did not change.
"""
import json
from pathlib import Path

import pytest

from csi.broadcast import (FORMAT_VERSION, BroadcastError, LinkReport, RoundBroadcast, decode_broadcast,
                           dequantise_score, encode_broadcast, quantise_score)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "round_broadcast_v2.json").read_text())
VALID = {case["name"]: case for case in GOLDEN["valid"]}
INVALID = {case["name"]: case for case in GOLDEN["invalid"]}
GOLDEN_V1 = json.loads((FIXTURES / "round_broadcast_v1.json").read_text())
VALID_V1 = {case["name"]: case for case in GOLDEN_V1["valid"]}


def as_fields(broadcast: RoundBroadcast) -> dict:
    """The decoded broadcast in the fixture's vocabulary."""
    return {
        "version": broadcast.version, "frame_type": broadcast.frame_type, "round": broadcast.round,
        "slot": broadcast.slot, "slots_per_round": broadcast.slots_per_round,
        "schedule_version": broadcast.schedule_version, "node_index": broadcast.node_index,
        "membership": broadcast.membership, "node_id": broadcast.node_id, "tag": broadcast.tag.hex(),
        "reports": [{"transmitter_index": r.transmitter_index, "score": r.score_q, "rssi": r.rssi,
                     "heard": r.heard, "since": r.since, "flags": r.flags} for r in broadcast.reports],
    }


def from_fields(fields: dict) -> RoundBroadcast:
    reports = tuple(LinkReport(r["transmitter_index"], r["score"], r["rssi"], r["heard"], r["since"], r["flags"])
                    for r in fields["reports"])
    return RoundBroadcast(fields["version"], fields["frame_type"], fields["round"], fields["slot"],
                          fields["slots_per_round"], fields["schedule_version"], fields["node_index"],
                          fields["membership"], fields["node_id"], bytes.fromhex(fields["tag"]), reports)


def test_the_format_version_is_2():
    assert FORMAT_VERSION == 2


@pytest.mark.parametrize("name", sorted(VALID_V1))
def test_v2_is_the_v1_layout_with_a_new_version_byte_and_a_real_tag(name):
    v1, v2 = bytes.fromhex(VALID_V1[name]["hex"]), bytes.fromhex(VALID[name]["hex"])
    assert (v1[0], v2[0]) == (1, 2)
    assert v2[1:18] + v2[26:] == v1[1:18] + v1[26:]
    fields_v1 = dict(VALID_V1[name]["broadcast"], version=2, tag=None)
    assert dict(VALID[name]["broadcast"], tag=None) == fields_v1


@pytest.mark.parametrize("name", sorted(VALID_V1))
def test_a_v1_broadcast_is_rejected_as_an_old_version(name):
    with pytest.raises(BroadcastError) as err:
        decode_broadcast(bytes.fromhex(VALID_V1[name]["hex"]))
    assert err.value.reason == "version"


@pytest.mark.parametrize("name", sorted(VALID))
def test_golden_bytes_decode_to_their_fields(name):
    case = VALID[name]
    assert as_fields(decode_broadcast(bytes.fromhex(case["hex"]))) == case["broadcast"]


@pytest.mark.parametrize("name", sorted(VALID))
def test_golden_fields_encode_to_their_bytes(name):
    case = VALID[name]
    assert encode_broadcast(from_fields(case["broadcast"])).hex() == case["hex"]


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_bytes_are_rejected_with_their_reason(name):
    case = INVALID[name]
    with pytest.raises(BroadcastError) as err:
        decode_broadcast(bytes.fromhex(case["hex"]))
    assert err.value.reason == case["reason"]


def test_link_report_flags_read_by_name():
    warming, = [r for r in decode_broadcast(bytes.fromhex(VALID["three_nodes_middle_slot"]["hex"])).reports
                if r.flags]
    assert (warming.warming, warming.possible_move, warming.raw_mode) == (True, False, False)
    moved, = decode_broadcast(bytes.fromhex(VALID["highest_and_lowest_index"]["hex"])).reports
    assert (moved.warming, moved.possible_move, moved.raw_mode) == (False, True, True)


def test_the_encoder_refuses_reports_that_do_not_follow_the_membership():
    broadcast = from_fields(VALID["three_nodes_middle_slot"]["broadcast"])
    with pytest.raises(ValueError):
        encode_broadcast(broadcast._replace(reports=broadcast.reports[:1]))
    with pytest.raises(ValueError):
        encode_broadcast(broadcast._replace(reports=tuple(reversed(broadcast.reports))))


def test_score_zero_means_no_score_yet():
    assert quantise_score(None) == 0
    assert dequantise_score(0) is None


@pytest.mark.parametrize("score", [1e-3, 0.05, 1.0, 7.3, 450.0, 9e3])
def test_a_score_survives_quantisation_within_a_few_percent(score):
    q = quantise_score(score)
    assert 1 <= q <= 255
    assert dequantise_score(q) == pytest.approx(score, rel=0.05)


def test_the_score_code_is_ten_steps_per_octave_from_two_to_the_minus_ten():
    # the C scorer ticket's score code: score = 2 ** ((code - 1) / 10 - 10)
    assert quantise_score(1.0) == 101 and dequantise_score(101) == 1.0
    assert quantise_score(2.0) == 111 and dequantise_score(111) == 2.0
    assert quantise_score(2.0 ** -10) == 1 and dequantise_score(1) == 2.0 ** -10
    assert dequantise_score(255) == pytest.approx(2.0 ** 15.4)


def test_quantisation_keeps_the_order_of_scores_and_saturates_at_the_ends():
    assert quantise_score(1.0) < quantise_score(2.0) < quantise_score(4.0)
    assert quantise_score(0.0) == 1          # a real but tiny score is not "no score"
    assert quantise_score(1e12) == 255
