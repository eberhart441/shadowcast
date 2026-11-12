"""Round broadcast format v2: the encoder and decoder against the golden byte fixtures (docs/round-broadcast.md).

The fixtures' layout bytes are written by hand and their tags computed with openssl, so they are the independent
source of truth that the firmware's encoder and decoder must also reproduce. v2 is v1's layout with a real
authentication tag (tests/test_round_auth.py); the frozen v1 fixtures pin that the layout did not change.
"""
import json
from pathlib import Path

import pytest

from csi.broadcast import (FORMAT_VERSION, FRAME_JOIN_GRANT, FRAME_JOIN_REQUEST, FRAME_RAW_REQUEST, FRAME_ROUND_RAW,
                           BroadcastError, LinkReport, RawCsi, RawModeRequest, RoundBroadcast, decode_broadcast, encode_broadcast)
from csi.score_code import decode_score, encode_score

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
                     "heard": r.heard, "since": r.since, "flags": r.flags, "lateness": r.lateness}
                    for r in broadcast.reports],
        "raw": [{"transmitter_index": c.transmitter_index, "round": c.round, "rssi": c.rssi, "iq": c.iq.hex()}
                for c in broadcast.raw],
        "request": None if broadcast.request is None else {
            "target": broadcast.request.target, "target_round": broadcast.request.target_round,
            "transmitters": list(broadcast.request.transmitters)},
    }


def with_defaults(fields: dict) -> dict:
    """A fixture's broadcast with the optional keys filled in: lateness 0 (none measured), no raw CSI, no request."""
    return {"raw": [], "request": None, **fields, "reports": [{"lateness": 0, **r} for r in fields["reports"]]}


def from_fields(fields: dict) -> RoundBroadcast:
    reports = tuple(LinkReport(r["transmitter_index"], r["score"], r["rssi"], r["heard"], r["since"], r["flags"],
                               r.get("lateness", 0)) for r in fields["reports"])
    return RoundBroadcast(fields["version"], fields["frame_type"], fields["round"], fields["slot"],
                          fields["slots_per_round"], fields["schedule_version"], fields["node_index"],
                          fields["membership"], fields["node_id"], bytes.fromhex(fields["tag"]), reports,
                          tuple(RawCsi(c["transmitter_index"], c["round"], c["rssi"], bytes.fromhex(c["iq"]))
                                for c in fields.get("raw", ())),
                          None if "request" not in fields else RawModeRequest(
                              fields["request"]["target"], fields["request"]["target_round"],
                              tuple(fields["request"]["transmitters"])))


def test_the_format_version_is_2():
    assert FORMAT_VERSION == 2


def test_every_v2_case_but_the_raw_mode_frames_has_a_v1_case():
    assert sorted(set(VALID) - set(VALID_V1)) == ["raw_csi_one_link", "raw_csi_two_links", "raw_mode_request_off",
                                                   "raw_mode_request_one_link"]


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
    assert as_fields(decode_broadcast(bytes.fromhex(case["hex"]))) == with_defaults(case["broadcast"])


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
    assert encode_score(None) == 0
    assert decode_score(0) is None


@pytest.mark.parametrize("score", [1e-3, 0.05, 1.0, 7.3, 450.0, 9e3])
def test_a_score_survives_quantisation_within_a_few_percent(score):
    q = encode_score(score)
    assert 1 <= q <= 255
    assert decode_score(q) == pytest.approx(score, rel=0.05)


def test_the_score_code_is_ten_steps_per_octave_from_two_to_the_minus_ten():
    # the C scorer ticket's score code: score = 2 ** ((code - 1) / 10 - 10)
    assert encode_score(1.0) == 101 and decode_score(101) == 1.0
    assert encode_score(2.0) == 111 and decode_score(111) == 2.0
    assert encode_score(2.0 ** -10) == 1 and decode_score(1) == 2.0 ** -10
    assert decode_score(255) == pytest.approx(2.0 ** 15.4)


def test_quantisation_keeps_the_order_of_scores_and_saturates_at_the_ends():
    assert encode_score(1.0) < encode_score(2.0) < encode_score(4.0)
    assert encode_score(0.0) == 1          # a real but tiny score is not "no score"
    assert encode_score(1e12) == 255


def test_slot_lateness_reads_in_microseconds():
    early, late = decode_broadcast(bytes.fromhex(VALID["slot_lateness_in_reports"]["hex"])).reports
    assert early.slot_lateness_us == 304    # code 10: 288 to 320 us, the middle
    assert late.slot_lateness_us == 960     # code 31: 960 us or more
    none, = decode_broadcast(bytes.fromhex(VALID["highest_and_lowest_index"]["hex"])).reports
    assert none.slot_lateness_us is None


def test_join_frames_are_header_only():
    request = decode_broadcast(bytes.fromhex(VALID["join_request"]["hex"]))
    grant = decode_broadcast(bytes.fromhex(VALID["join_grant"]["hex"]))
    assert (request.frame_type, request.is_join_request, request.is_join_grant) == (FRAME_JOIN_REQUEST, True, False)
    assert (grant.frame_type, grant.is_join_request, grant.is_join_grant) == (FRAME_JOIN_GRANT, False, True)
    assert request.reports == grant.reports == ()


def test_the_encoder_refuses_a_lateness_code_over_31():
    broadcast = from_fields(VALID["slot_lateness_in_reports"]["broadcast"])
    report = broadcast.reports[0]._replace(lateness=32)
    with pytest.raises(ValueError):
        encode_broadcast(broadcast._replace(reports=(report,) + broadcast.reports[1:]))


def test_raw_csi_rides_after_the_link_reports():
    broadcast = decode_broadcast(bytes.fromhex(VALID["raw_csi_two_links"]["hex"]))
    assert broadcast.frame_type == FRAME_ROUND_RAW and broadcast.carries_reports
    assert [r.raw_mode for r in broadcast.reports] == [True, True]
    first, second = broadcast.raw
    assert (first.transmitter_index, first.round, first.rssi) == (0, 1111, -50)
    assert first.iq == bytes([0x0a, 0x0b, 0xf6, 0xf5])
    assert first.values == [10, 11, -10, -11]          # int8, in the driver's order
    assert second.values == [127, -127]


def test_a_raw_mode_request_names_the_target_and_its_transmitters():
    request = decode_broadcast(bytes.fromhex(VALID["raw_mode_request_one_link"]["hex"]))
    assert request.frame_type == FRAME_RAW_REQUEST and not request.carries_reports
    assert request.request == RawModeRequest("02:00:00:00:00:02", 4660, ("02:00:00:00:00:01",))
    off = decode_broadcast(bytes.fromhex(VALID["raw_mode_request_off"]["hex"]))
    assert off.request.transmitters == ()


def test_the_encoder_refuses_raw_csi_it_could_not_decode():
    broadcast = from_fields(VALID["raw_csi_one_link"]["broadcast"])
    block = broadcast.raw[0]
    for bad in (block._replace(iq=b""), block._replace(iq=bytes(130)), block._replace(transmitter_index=1)):
        with pytest.raises(ValueError):
            encode_broadcast(broadcast._replace(raw=(bad,)))
    with pytest.raises(ValueError):
        encode_broadcast(broadcast._replace(raw=(block, block, block)))
