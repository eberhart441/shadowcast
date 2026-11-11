"""Ingest never drops silently: several gateways, rejection counters, unknown nodes, an overloaded consumer.

Round broadcasts go in through DashboardState.push_broadcast (tagged with the gateway that heard them) and every
assertion is on the payload's ``status.broadcasts`` and ``status.messages``.
"""
import json
import urllib.request
from pathlib import Path

import pytest

from csi.broadcast import LinkReport, RoundBroadcast, encode_broadcast, quantise_score
from csi.dashboard import DashboardState
from csi.room import load_room
from csi.server import DashboardServer

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NAMES = [f"n{k}" for k in range(1, 9)]
SERIAL = {name: f"02:00:00:00:{k:02X}:5C" for k, name in enumerate(NAMES, 1)}
ALL = (1 << len(NAMES)) - 1
ROUND_HZ = 20.0
T0 = 1_700_000_000.0


def broadcast(sender, round_no, *, node_id=None, schedule=1):
    index = NAMES.index(sender)
    rows = tuple(LinkReport(i, quantise_score(2.0), -50, 15, 0) for i in range(len(NAMES)) if i != index)
    return encode_broadcast(RoundBroadcast(1, 1, round_no % 65536, index, len(NAMES) + 1, schedule, index, ALL,
                                           node_id or SERIAL[sender], bytes(8), rows))


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
    state.push_broadcast(T0 + 0.1, broadcast("n1", 7, schedule=2), "gw2")  # a new schedule restarted rounds
    state.push_broadcast(T0 + 3300.0, broadcast("n1", 7), "gw1")           # the u16 round number wrapped
    counts = state.snapshot(T0 + 3300.0)["status"]["broadcasts"]
    assert counts["accepted"] == 4
    assert counts["rejected"]["duplicate"] == 1


def test_every_rejection_reason_has_a_counter_from_the_start():
    counts = DashboardState(ROOM, mode="auto").snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"] == {"short": 0, "version": 0, "frame_type": 0, "length": 0, "membership": 0,
                                  "duplicate": 0, "queue_overflow": 0}


def test_each_malformed_or_wrong_version_record_is_counted_by_its_reason():
    good = broadcast("n1", 0)
    state = DashboardState(ROOM, mode="auto")
    for data in (good[:10],                              # short
                 b"\x02" + good[1:],                     # version
                 good[:1] + b"\x07" + good[2:],          # frame_type
                 good + b"\x00",                         # length
                 good[:7] + b"\x09" + good[8:]):         # membership: node index 9 is not a member
        state.push_broadcast(T0, data, "gw1")
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"] == {"short": 1, "version": 1, "frame_type": 1, "length": 1, "membership": 1,
                                  "duplicate": 0, "queue_overflow": 0}
    assert counts["accepted"] == 0 and counts["heard"] == 5


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
    assert any("unknown node" in m and stranger in m and "config/room.json" in m
               for m in payload["status"]["messages"])


def test_unknown_serials_are_counted_in_bounded_memory():
    state = DashboardState(ROOM, mode="auto")
    for k in range(200):
        state.push_broadcast(T0 + k, broadcast("n1", 0, node_id=f"02:00:00:01:{k:02X}:00"), "gw1")
    unknown = state.snapshot(T0 + 200)["status"]["broadcasts"]["unknown_nodes"]
    assert len(unknown) <= 33
    assert sum(unknown.values()) == 200
