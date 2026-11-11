"""Ingest never drops silently: several gateways, rejection counters, unknown nodes, an overloaded consumer.

Round broadcasts go in through DashboardState.push_broadcast (tagged with the gateway that heard them) and every
assertion is on the payload's ``status.broadcasts`` and ``status.messages``.
"""
import json
import time
import urllib.request
from pathlib import Path

import pytest

from csi.broadcast import FORMAT_VERSION, LinkReport, RoundBroadcast, encode_broadcast, quantise_score
from csi.dashboard import DashboardState
from csi.round_auth import TEST_KIT_KEY, sign_broadcast
from csi.room import Walk, load_room
from csi.server import DashboardServer, broadcast_messages
from csi.simulate import simulate_gateways, simulate_rounds

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NAMES = [f"n{k}" for k in range(1, 9)]
SERIAL = {name: f"02:00:00:00:{k:02X}:5C" for k, name in enumerate(NAMES, 1)}
ALL = (1 << len(NAMES)) - 1
ROUND_HZ = 20.0
T0 = 1_700_000_000.0


def broadcast(sender, round_no, *, node_id=None, schedule=1, key=TEST_KIT_KEY, score=2.0):
    index = NAMES.index(sender)
    rows = tuple(LinkReport(i, quantise_score(score), -50, 15, 0) for i in range(len(NAMES)) if i != index)
    return sign_broadcast(encode_broadcast(RoundBroadcast(FORMAT_VERSION, 1, round_no % 65536, index, len(NAMES) + 1,
                                                          schedule, index, ALL, node_id or SERIAL[sender], bytes(8),
                                                          rows)), key)


def rounds(seconds, senders=NAMES):
    """(host_ts, sender, bytes) for every sender's broadcast, once per round, in host-time order."""
    out = []
    for round_no in range(int(round(seconds * ROUND_HZ))):
        for slot, sender in enumerate(NAMES):
            if sender in senders:
                host_ts = T0 + round_no / ROUND_HZ + slot / (ROUND_HZ * (len(NAMES) + 1))
                out.append((host_ts, sender, broadcast(sender, round_no)))
    return out


def links(payload):
    return {row["id"]: row for row in payload["links"]}


def test_two_gateways_hearing_overlapping_nodes_count_each_link_report_once():
    gw1, gw2 = set(NAMES[:6]), set(NAMES[2:])          # both hear n3..n6
    single, merged = DashboardState(ROOM, mode="auto"), DashboardState(ROOM, mode="auto")
    for host_ts, sender, data in rounds(3.0):
        single.push_broadcast(host_ts, data, "gw")
        if sender in gw1:
            merged.push_broadcast(host_ts, data, "gw1")
        if sender in gw2:
            merged.push_broadcast(host_ts + 0.002, data, "gw2")   # the second gateway's copy, 2 ms later
    one, two = single.snapshot(T0 + 3.0), merged.snapshot(T0 + 3.0)
    rate = {k: v["pkt_rate"] for k, v in links(one).items()}
    assert rate["n1<-n8"] == rate["n4<-n5"] == pytest.approx(ROUND_HZ, rel=0.1)   # one report per round, not two
    assert {k: v["pkt_rate"] for k, v in links(two).items()} == rate
    counts = two["status"]["broadcasts"]
    assert counts["accepted"] == 8 * 60
    assert counts["rejected"]["duplicate"] == 4 * 60
    assert counts["sources"]["gw1"] == {"heard": 6 * 60, "accepted": 6 * 60, "duplicate": 0, "queue_overflow": 0}
    assert counts["sources"]["gw2"] == {"heard": 6 * 60, "accepted": 2 * 60, "duplicate": 4 * 60,
                                        "queue_overflow": 0}


def test_a_copy_is_a_duplicate_only_for_the_same_node_schedule_and_round_within_two_seconds():
    state = DashboardState(ROOM, mode="auto")
    state.push_broadcast(T0, broadcast("n1", 7), "gw1")
    state.push_broadcast(T0 + 1.9, broadcast("n1", 7), "gw2")              # the same round, late: a copy
    state.push_broadcast(T0 + 0.1, broadcast("n2", 7), "gw2")              # another node, same round
    state.push_broadcast(T0 + 0.1, broadcast("n1", 7, schedule=2), "gw2")  # not a copy: a new schedule, but
    #                                                    the same round number again from n1 is a replay
    state.push_broadcast(T0 + 3300.0, broadcast("n1", 7), "gw1")           # the u16 round number wrapped
    counts = state.snapshot(T0 + 3300.0)["status"]["broadcasts"]
    assert counts["accepted"] == 3
    assert counts["rejected"]["duplicate"] == 1
    assert counts["rejected"]["replay"] == 1


def test_every_rejection_reason_has_a_counter_from_the_start():
    counts = DashboardState(ROOM, mode="auto").snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"] == {"short": 0, "version": 0, "frame_type": 0, "length": 0, "membership": 0,
                                  "auth": 0, "replay": 0, "duplicate": 0, "queue_overflow": 0}


def test_each_malformed_or_wrong_version_record_is_counted_by_its_reason():
    good = broadcast("n1", 0)
    state = DashboardState(ROOM, mode="auto")
    for data in (good[:10],                              # short
                 b"\x01" + good[1:],                     # version: a v1 node (old firmware)
                 good[:1] + b"\x07" + good[2:],          # frame_type
                 good + b"\x00",                         # length
                 good[:7] + b"\x09" + good[8:]):         # membership: node index 9 is not a member
        state.push_broadcast(T0, data, "gw1")
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"] == {"short": 1, "version": 1, "frame_type": 1, "length": 1, "membership": 1,
                                  "auth": 0, "replay": 0, "duplicate": 0, "queue_overflow": 0}
    assert counts["accepted"] == 0 and counts["heard"] == 5


def test_a_forged_broadcast_is_counted_as_auth_and_moves_no_link():
    state = DashboardState(ROOM, mode="auto")
    for host_ts, _, data in rounds(4.0):
        state.push_broadcast(host_ts, data, "gw1")
    before = state.snapshot(T0 + 4.0)
    real = broadcast("n1", 80, score=500.0)
    forged = real[:-1] + bytes([real[-1] ^ 0x01])                  # one flag bit changed after signing
    state.push_broadcast(T0 + 4.0, forged, "gw1")
    state.push_broadcast(T0 + 4.0, broadcast("n2", 80, score=500.0, key=bytes(16)), "gw1")   # another kit's key
    after = state.snapshot(T0 + 4.0)
    counts = after["status"]["broadcasts"]
    assert counts["rejected"]["auth"] == 2
    assert counts["accepted"] == 8 * 80 and counts["heard"] == 8 * 80 + 2
    assert {r["id"]: r["state"] for r in after["links"]} == {r["id"]: r["state"] for r in before["links"]}
    assert any("auth 2" in m and "kit key" in m for m in broadcast_messages(counts))


def test_a_forged_broadcast_cannot_take_the_place_of_the_real_one():
    state = DashboardState(ROOM, mode="auto")
    real = broadcast("n1", 5)
    state.push_broadcast(T0, real[:-1] + bytes([real[-1] ^ 0xFF]), "gw1")   # a forged copy first: same node, round
    state.push_broadcast(T0 + 0.001, real, "gw2")
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert (counts["accepted"], counts["rejected"]["auth"], counts["rejected"]["duplicate"]) == (1, 1, 0)


def test_a_replayed_broadcast_is_counted_as_replay_and_not_used():
    state = DashboardState(ROOM, mode="auto")
    live = rounds(6.0)
    for host_ts, _, data in live:
        state.push_broadcast(host_ts, data, "gw1")
    _, _, old = live[8 * 10]                                        # n1's round 10, recorded 5.5 s ago
    state.push_broadcast(T0 + 6.0, old, "gw1")
    counts = state.snapshot(T0 + 6.0)["status"]["broadcasts"]
    assert counts["rejected"]["replay"] == 1 and counts["rejected"]["duplicate"] == 0
    assert counts["accepted"] == len(live)
    assert any("replay 1" in m for m in broadcast_messages(counts))


def test_a_node_that_restarts_its_round_numbers_is_taken_again_after_two_silent_seconds():
    state = DashboardState(ROOM, mode="auto")
    for k in range(40):
        state.push_broadcast(T0 + k / ROUND_HZ, broadcast("n1", 1000 + k), "gw1")
    restart = T0 + 2.0 + 2.5                                        # n1 rebooted and starts again at round 0
    for k in range(10):
        state.push_broadcast(restart + k / ROUND_HZ, broadcast("n1", k), "gw1")
    counts = state.snapshot(restart + 1.0)["status"]["broadcasts"]
    assert counts["accepted"] == 50 and counts["rejected"]["replay"] == 0


def test_the_host_verifies_with_the_kit_key_it_is_given():
    kit = bytes(range(100, 116))
    state = DashboardState(ROOM, mode="auto", kit_key=kit)
    state.push_broadcast(T0, broadcast("n1", 1, key=kit), "gw1")
    state.push_broadcast(T0, broadcast("n2", 1), "gw1")             # the test key: not this kit
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert (counts["accepted"], counts["rejected"]["auth"]) == (1, 1)


def get_state(server):
    with urllib.request.urlopen(server.url + "/api/state", timeout=10) as response:
        return json.loads(response.read())


def serve(sources, **kwargs):
    """Run DashboardServer over finite ``sources`` to the end; the final /api/state payload."""
    server = DashboardServer(ROOM, sources, mode="auto", port=0, **kwargs)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
        return get_state(server)
    finally:
        server.stop()


def test_a_serial_missing_from_the_room_config_shows_as_an_unknown_node():
    stranger = "02:00:00:00:99:5C"
    stream = [(T0 + i / ROUND_HZ, broadcast("n1", i, node_id=stranger)) for i in range(20)]
    stream += [(host_ts + 2.0, data) for host_ts, _, data in rounds(1.0)]
    payload = serve({"gw1": iter(sorted(stream))})
    assert payload["status"]["broadcasts"]["unknown_nodes"] == {stranger: 20}
    assert payload["status"]["broadcasts"]["accepted"] == 8 * 20
    assert any("unknown node" in m and stranger in m and "room config" in m
               for m in payload["status"]["messages"])


def test_unknown_serials_are_counted_in_bounded_memory():
    state = DashboardState(ROOM, mode="auto")
    for k in range(200):
        state.push_broadcast(T0 + k, broadcast("n1", 0, node_id=f"02:00:00:01:{k:02X}:00"), "gw1")
    unknown = state.snapshot(T0 + 200)["status"]["broadcasts"]["unknown_nodes"]
    assert len(unknown) <= 33
    assert sum(unknown.values()) == 200


def test_an_overloaded_consumer_drops_live_records_with_a_count_and_bounded_memory(monkeypatch):
    monkeypatch.setattr("csi.multisource.MAX_QUEUED_BROADCASTS", 16)
    slow = DashboardState.push_broadcast

    def overloaded(self, *args, **kwargs):          # the consumer takes 2 ms a record; the gateway sends faster
        time.sleep(0.002)
        slow(self, *args, **kwargs)

    monkeypatch.setattr(DashboardState, "push_broadcast", overloaded)
    now = time.time()
    live = [(now + host_ts - T0, data) for host_ts, _, data in rounds(2.0)]   # 320 records, stamped live
    payload = serve({"gw1": iter(live)})
    counts = payload["status"]["broadcasts"]
    dropped = counts["rejected"]["queue_overflow"]
    assert dropped > 0
    assert counts["sources"]["gw1"]["queue_overflow"] == dropped
    assert counts["heard"] == len(live) == counts["accepted"] + dropped     # every record accounted for
    assert any("dropped" in m and "fell behind" in m for m in payload["status"]["messages"])


def test_two_simulated_gateways_hearing_overlapping_nodes_give_the_links_of_one():
    walk = Walk(((1.0, 1.5), (3.0, 1.5)), speed_mps=0.4, loop=True)
    kwargs = dict(duration_s=6.0, quiet_first_s=3.0, seed=5, t0=T0)
    one = serve({"gw": simulate_rounds(ROOM, walk, **kwargs)})
    two = serve(simulate_gateways(ROOM, walk, hears={"gw1": NAMES[:6], "gw2": NAMES[2:]}, **kwargs))
    rate = {r["id"]: r["pkt_rate"] for r in one["links"]}
    assert {r["id"]: r["pkt_rate"] for r in two["links"]} == pytest.approx(rate, abs=1.0)   # 40 if counted twice
    assert {r["id"]: r["state"] for r in two["links"]} == {r["id"]: r["state"] for r in one["links"]}
    counts = two["status"]["broadcasts"]
    assert counts["accepted"] == one["status"]["broadcasts"]["accepted"] == 8 * 6 * 20
    assert counts["rejected"]["duplicate"] == 4 * 6 * 20              # n3..n6, heard by both
    assert counts["sources"]["gw1"]["heard"] == counts["sources"]["gw2"]["heard"] == 6 * 6 * 20
    assert two["status"]["receivers"] == {"gw1": "finished", "gw2": "finished"}


def join_frame(frame_type, joiner, index, membership, *, schedule=1, round_no=0, key=TEST_KIT_KEY):
    return sign_broadcast(encode_broadcast(RoundBroadcast(FORMAT_VERSION, frame_type, round_no, 8, 9, schedule, index,
                                                          membership, SERIAL[joiner], bytes(8), ())), key)


def test_join_frames_are_counted_apart_and_never_placed_or_rejected():
    without_n8 = ALL & ~(1 << 7)
    state = DashboardState(ROOM, mode="auto")
    state.push_broadcast(T0, join_frame(2, "n8", 7, without_n8), "gw1")              # n8 asks to join
    state.push_broadcast(T0 + 0.002, join_frame(2, "n8", 7, without_n8), "gw2")      # a second gateway's copy
    state.push_broadcast(T0 + 0.05, join_frame(3, "n8", 7, ALL, schedule=2), "gw1")  # the scheduler admits it
    counts = state.snapshot(T0 + 0.1)["status"]["broadcasts"]
    assert counts["join_frames"] == {"request": 2, "grant": 1}
    assert counts["accepted"] == 0 and sum(counts["rejected"].values()) == 0 and counts["heard"] == 3
    assert broadcast_messages(counts) == []


def test_a_join_grant_names_the_joiner_so_reports_about_it_place_before_its_own_broadcast():
    state = DashboardState(ROOM, mode="auto")
    state.push_broadcast(T0, join_frame(3, "n8", 7, ALL, schedule=5), "gw1")
    state.push_broadcast(T0 + 0.01, broadcast("n1", 1, schedule=5), "gw1")
    payload = state.snapshot(T0 + 0.01)
    assert payload["status"]["broadcasts"]["unplaced_reports"] == 6       # n2..n7 not heard yet; n8 is named
    assert links(payload)["n1<-n8"]["rssi"] == -50


def test_a_forged_join_frame_is_rejected_as_auth():
    state = DashboardState(ROOM, mode="auto")
    state.push_broadcast(T0, join_frame(2, "n8", 7, ALL & ~(1 << 7), key=bytes(range(1, 17))), "gw1")
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"]["auth"] == 1 and counts["join_frames"] == {"request": 0, "grant": 0}


def test_every_record_is_accounted_for_including_what_the_gateway_lost():
    """#7's accounting, extended past the host: a gateway's own losses (its queue, its USB writes, CSI it skipped)
    and the records that failed their CRC on the way in are in status.broadcasts.gateways and status.messages."""
    from csi.gateway_records import COUNTER_NAMES, GatewaySource, encode_heard, encode_status

    good = rounds(1.0)
    losses = {"queue_full": 4, "usb_dropped": 2, "csi_skipped": 1}
    status = encode_status("02:00:00:00:00:80", 1_000_000, 1, 1, 6,
                           [losses.get(name, 0) for name in COUNTER_NAMES])
    corrupt = bytearray(encode_heard(5, -40, good[0][2]))
    corrupt[-1] ^= 0xFF                                   # its CRC fails: the reader skips it and counts it
    chunks = [(host_ts, encode_heard(k * 3125, -40, data)) for k, (host_ts, _, data) in enumerate(good)]
    chunks.insert(3, (chunks[2][0], bytes(corrupt) + b"boot rom noise"))
    chunks.append((chunks[-1][0], status))
    payload = serve({"gw1": GatewaySource(iter(chunks))})
    counts = payload["status"]["broadcasts"]
    assert counts["heard"] == len(good) == counts["accepted"]
    gw = counts["gateways"]["gw1"]
    assert {k: gw[k] for k in losses} == losses
    assert gw["records_rejected"] == {"crc": 1}
    assert gw["skipped_bytes"] > 0
    lines = [m for m in payload["status"]["messages"] if "gateway gw1" in m]
    assert lines and all(word in lines[0] for word in ("queue_full 4", "usb_dropped 2", "csi_skipped 1", "crc 1"))
