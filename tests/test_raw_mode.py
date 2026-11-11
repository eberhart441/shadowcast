"""Raw mode (ticket #17) at the host's seams: round broadcasts in through DashboardState.push_broadcast, raw mode
requests out of DashboardState.raw_mode_request, and the payload's ``raw_mode`` and ``status.broadcasts``.

The bytes a node sends are built here by hand from the format (docs/round-broadcast.md); the request frames the
host sends are read back with the decoder that the golden fixtures pin.
"""
from pathlib import Path

import pytest

from csi.broadcast import (FLAG_RAW_MODE, FORMAT_VERSION, FRAME_RAW_REQUEST, FRAME_ROUND, FRAME_ROUND_RAW,
                           RAW_REQUEST_MAX_AGE_ROUNDS, LinkReport, RawCsi, RawModeRequest, RoundBroadcast,
                           decode_broadcast, encode_broadcast, quantise_score)
from csi.dashboard import DashboardState
from csi.raw_mode import (ACK_TIMEOUT_S, RAW_BUDGET_BYTES, REQUEST_COPIES, RawModeRefused, ppdu_us,
                          raw_airtime_us)
from csi.room import load_room
from csi.round_auth import TEST_KIT_KEY, sign_broadcast, tag_ok

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NAMES = [f"n{k}" for k in range(1, 9)]
SERIAL = {name: f"02:00:00:00:{k:02X}:5C" for k, name in enumerate(NAMES, 1)}
ALL = (1 << len(NAMES)) - 1
GATEWAY = "02:00:00:00:00:80"
T0 = 1_700_000_000.0
IQ = bytes(range(64)) * 2   # 128 bytes, a full HT20 long training field


def broadcast(sender, round_no, *, raw=(), flagged=()):
    """``sender``'s round broadcast; ``raw`` names transmitters whose raw CSI it carries, ``flagged`` those whose
    link reports carry the raw mode flag (by default the ``raw`` ones)."""
    index = NAMES.index(sender)
    flagged = set(flagged) | set(raw)
    rows = tuple(LinkReport(i, quantise_score(2.0), -50, 15, 0, FLAG_RAW_MODE if NAMES[i] in flagged else 0)
                 for i in range(len(NAMES)) if i != index)
    blocks = tuple(RawCsi(NAMES.index(t), round_no, -48, IQ) for t in raw)
    return sign_broadcast(encode_broadcast(RoundBroadcast(
        FORMAT_VERSION, FRAME_ROUND_RAW if blocks else FRAME_ROUND, round_no % 65536, index, len(NAMES) + 1, 1, index,
        ALL, SERIAL[sender], bytes(8), rows, blocks)), TEST_KIT_KEY)


def heard_round(state, round_no, *, raw=None, flagged=None, t=None):
    """One round: every node's round broadcast, ``raw`` / ``flagged`` as {sender: transmitters}."""
    t = T0 + round_no * 0.05 if t is None else t
    for slot, name in enumerate(NAMES):
        state.push_broadcast(t + slot * 0.005, broadcast(name, round_no, raw=(raw or {}).get(name, ()),
                                                         flagged=(flagged or {}).get(name, ())), "gw")
    return t + 0.045


def raw_rows(payload):
    return {row["id"]: row for row in payload["raw_mode"]["links"]}


def test_report_03s_airtime_model_gives_its_own_table():
    # 03-rf-airtime.md section 2.2, row HT20 MCS0: the 4-byte sensing frame, 250 B of raw I/Q, an 871 B ASCII line
    assert (ppdu_us(4), ppdu_us(250), ppdu_us(871)) == (106, 406, 1198)


def test_one_raw_link_costs_about_one_percent_of_airtime_at_most():
    # one raw link adds 172 us to a 30-node round broadcast; the whole budget (3 links) about 1 % at 20 Hz
    assert raw_airtime_us(30) == ppdu_us(142 + 133) - ppdu_us(142) == 172
    assert RAW_BUDGET_BYTES // 133 == 3
    assert raw_airtime_us(30, 3) * 20 / 1e6 == pytest.approx(0.0103, abs=0.0001)
    assert raw_airtime_us(16, 3) * 40 / 1e6 == pytest.approx(0.0197, abs=0.0001)


def test_a_round_broadcast_with_raw_csi_drives_its_links_and_counts_the_csi_on_its_link():
    plain, raw = DashboardState(ROOM, mode="auto"), DashboardState(ROOM, mode="auto")
    for k in range(40):
        heard_round(plain, k)
        now = heard_round(raw, k, raw={"n1": ["n5"]})
    links = lambda p: {row["id"]: (row["state"], row["pkt_rate"]) for row in p["links"]}   # noqa: E731
    assert links(raw.snapshot(now)) == links(plain.snapshot(now))
    payload = raw.snapshot(now)
    counts = payload["status"]["broadcasts"]
    assert counts["accepted"] == 8 * 40
    # round 0: n1 (slot 0) sends before the host has heard n5 (slot 4), so n5's index is not known yet
    assert counts["raw_csi"] == {"blocks": 39, "iq_bytes": 39 * 128, "unplaced": 1}
    row = raw_rows(payload)["n1<-n5"]
    assert (row["state"], row["blocks"], row["iq_bytes"]) == ("on", 39, 39 * 128)
    assert row["rate_hz"] == pytest.approx(20.0, rel=0.1)
    assert payload["raw_mode"]["used_bytes"] == 133


def test_a_raw_mode_request_heard_from_another_gateway_is_counted_and_not_used():
    state = DashboardState(ROOM, mode="auto")
    heard_round(state, 0)
    frame, = state.raw_mode_request("n1<-n5", True, T0 + 1, sender_id=GATEWAY)[:1]
    state.push_broadcast(T0 + 1, frame, "gw2")
    counts = state.snapshot(T0 + 1)["status"]["broadcasts"]
    assert counts["raw_requests"] == 1
    assert counts["heard"] == counts["accepted"] + sum(counts["rejected"].values()) + sum(
        counts["unknown_nodes"].values()) + sum(counts["join_frames"].values()) + counts["raw_requests"]


def test_a_request_names_the_receiving_node_its_round_and_the_transmitter_and_is_signed():
    state = DashboardState(ROOM, mode="auto")
    heard_round(state, 300)
    frames = state.raw_mode_request("n1<-n5", True, T0 + 16, sender_id=GATEWAY)
    assert len(frames) == REQUEST_COPIES
    decoded = [decode_broadcast(f) for f in frames]
    assert all(tag_ok(f, TEST_KIT_KEY) for f in frames)
    assert {(d.frame_type, d.node_id) for d in decoded} == {(FRAME_RAW_REQUEST, GATEWAY)}
    assert {d.request for d in decoded} == {RawModeRequest(SERIAL["n1"], 300, (SERIAL["n5"],))}
    rounds = [d.round for d in decoded]
    assert rounds == [(rounds[0] + k) % 65536 for k in range(REQUEST_COPIES)]   # each copy fresh, never back
    again = decode_broadcast(state.raw_mode_request("n1<-n6", True, T0 + 17, sender_id=GATEWAY)[0])
    assert again.round == (rounds[-1] + 1) % 65536
    assert again.request.transmitters == (SERIAL["n5"], SERIAL["n6"])   # the node's whole set, every time
    off = decode_broadcast(state.raw_mode_request("n1<-n5", False, T0 + 18, sender_id=GATEWAY)[0])
    assert off.request.transmitters == (SERIAL["n6"],)
    assert RAW_REQUEST_MAX_AGE_ROUNDS >= 32   # room for the gateway's wait for a join slot


def test_a_request_beyond_the_byte_budget_is_refused_with_a_reason():
    state = DashboardState(ROOM, mode="auto")
    heard_round(state, 0)
    for label in ("n1<-n5", "n2<-n5", "n3<-n5"):
        state.raw_mode_request(label, True, T0 + 1, sender_id=GATEWAY)
    with pytest.raises(RawModeRefused) as refused:
        state.raw_mode_request("n4<-n5", True, T0 + 1, sender_id=GATEWAY)
    assert refused.value.reason == "budget"
    assert "400" in str(refused.value) and "n1<-n5" in str(refused.value)
    assert state.snapshot(T0 + 1)["raw_mode"]["used_bytes"] == 3 * 133
    state.raw_mode_request("n1<-n5", False, T0 + 2, sender_id=GATEWAY)
    state.raw_mode_request("n4<-n5", True, T0 + 2, sender_id=GATEWAY)     # room again after one is off


def test_a_node_streams_at_most_two_links():
    state = DashboardState(ROOM, mode="auto")
    heard_round(state, 0)
    state.raw_mode_request("n1<-n5", True, T0 + 1, sender_id=GATEWAY)
    state.raw_mode_request("n1<-n6", True, T0 + 1, sender_id=GATEWAY)
    with pytest.raises(RawModeRefused) as refused:
        state.raw_mode_request("n1<-n7", True, T0 + 1, sender_id=GATEWAY)
    assert refused.value.reason == "node_limit"


def test_links_a_node_already_streams_count_against_the_budget():
    state = DashboardState(ROOM, mode="auto")
    for k in range(2):   # e.g. left on by an earlier run of the dashboard
        now = heard_round(state, k, raw={"n1": ["n5"], "n2": ["n5"], "n3": ["n5"]})
    with pytest.raises(RawModeRefused) as refused:
        state.raw_mode_request("n4<-n5", True, now, sender_id=GATEWAY)
    assert refused.value.reason == "budget"


@pytest.mark.parametrize("label, sender, heard, reason", [
    ("n1<-n1", GATEWAY, True, "unknown_link"),
    ("n1<-x9", GATEWAY, True, "unknown_link"),
    ("nonsense", GATEWAY, True, "unknown_link"),
    ("n1<-n5", GATEWAY, False, "not_heard"),
    ("n1<-n5", None, True, "no_gateway"),
])
def test_requests_that_cannot_be_sent_are_refused_with_a_reason(label, sender, heard, reason):
    state = DashboardState(ROOM, mode="auto")
    if heard:
        heard_round(state, 0)
    with pytest.raises(RawModeRefused) as refused:
        state.raw_mode_request(label, True, T0 + 1, sender_id=sender)
    assert refused.value.reason == reason
    assert state.snapshot(T0 + 1)["raw_mode"]["links"] == []


def test_a_link_shows_requested_then_on_then_stopping_then_off():
    state = DashboardState(ROOM, mode="auto")
    now = heard_round(state, 0)
    state.raw_mode_request("n1<-n5", True, now, sender_id=GATEWAY)
    assert raw_rows(state.snapshot(now))["n1<-n5"]["state"] == "requested"
    now = heard_round(state, 1, raw={"n1": ["n5"]})
    assert raw_rows(state.snapshot(now))["n1<-n5"]["state"] == "on"
    state.raw_mode_request("n1<-n5", False, now, sender_id=GATEWAY)
    now = heard_round(state, 2, raw={"n1": ["n5"]})
    assert raw_rows(state.snapshot(now))["n1<-n5"]["state"] == "stopping"
    now = heard_round(state, 3)
    payload = state.snapshot(now)
    assert (raw_rows(payload)["n1<-n5"]["state"], payload["raw_mode"]["used_bytes"]) == ("off", 0)


def test_a_request_without_an_answer_says_so():
    state = DashboardState(ROOM, mode="auto")
    now = heard_round(state, 0)
    state.raw_mode_request("n1<-n5", True, now, sender_id=GATEWAY)
    k = 1
    while now < T0 + ACK_TIMEOUT_S + 0.5:
        now = heard_round(state, k)
        k += 1
    payload = state.snapshot(now)
    assert raw_rows(payload)["n1<-n5"]["state"] == "no answer"


def test_the_simulated_rig_streams_raw_csi_for_a_link_without_changing_anything_else():
    from csi.simulate import simulate_rounds
    plain = list(simulate_rounds(ROOM, None, duration_s=1.5, seed=4))
    raw = list(simulate_rounds(ROOM, None, duration_s=1.5, seed=4, raw={"n1": ["n5"]}))
    assert [t for t, _ in raw] == [t for t, _ in plain]
    for (_, a), (_, b) in zip(plain, raw):
        a, b = decode_broadcast(a), decode_broadcast(b)
        assert [r._replace(flags=0) for r in a.reports] == [r._replace(flags=0) for r in b.reports]
        if b.node_id == SERIAL["n1"]:
            assert b.frame_type == FRAME_ROUND_RAW
            assert [c.transmitter_index for c in b.raw] == [NAMES.index("n5")]
            assert [r.raw_mode for r in b.reports] == [r.transmitter_index == NAMES.index("n5") for r in b.reports]
        else:
            assert (b.frame_type, b.raw) == (FRAME_ROUND, ())


def test_a_simulated_node_takes_a_signed_fresh_request_for_itself_only():
    from collections import deque
    from csi.simulate import simulate_rounds
    state = DashboardState(ROOM, mode="auto")
    commands = deque()
    stream = simulate_rounds(ROOM, None, duration_s=3.0, seed=4, commands=commands)
    raw_from = {}
    for k, (host_ts, data) in enumerate(stream):
        state.push_broadcast(host_ts, data, "sim")
        if k == 8 * 10:     # round 10: ask n2 for raw mode on n2<-n7; and send a forged copy for n3
            frames = state.raw_mode_request("n2<-n7", True, host_ts, sender_id=GATEWAY)
            forged = bytearray(state.raw_mode_request("n3<-n7", True, host_ts, sender_id=GATEWAY)[0])
            forged[-1] ^= 1
            commands.extend(frames + [bytes(forged)])
        b = decode_broadcast(data)
        if b.raw:
            raw_from.setdefault(b.node_id, k // 8)
    assert raw_from == {SERIAL["n2"]: 11}
