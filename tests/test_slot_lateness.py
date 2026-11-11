"""Slot lateness per node reaches the dashboard (ticket #14).

Each receiving node measures how far into its slot every other member's round broadcast started on air and sends
it in the link report (bits 3-7 of the flags byte, docs/round-broadcast.md). The payload's
``node_health[].slot_lateness`` for a node is the median of what the other nodes report about it, in microseconds,
over the last 2 s; ``"unknown"`` when no node has reported it.
"""
from pathlib import Path

from csi.broadcast import LinkReport, RoundBroadcast, encode_broadcast, quantise_score
from csi.dashboard import DashboardState
from csi.room import load_room

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NAMES = [f"n{k}" for k in range(1, 9)]
SERIAL = {name: f"02:00:00:00:{k:02X}:5C" for k, name in enumerate(NAMES, 1)}
ALL = (1 << len(NAMES)) - 1
T0 = 1_700_000_000.0


def broadcast(sender, round_no, lateness=None):
    """``sender``'s round broadcast; ``lateness`` maps a transmitter name to the lateness code it reports."""
    index = NAMES.index(sender)
    lateness = lateness or {}
    rows = tuple(LinkReport(i, quantise_score(2.0), -50, 15, 0, 0, lateness.get(NAMES[i], 0))
                 for i in range(len(NAMES)) if i != index)
    return encode_broadcast(RoundBroadcast(1, 1, round_no, index, len(NAMES) + 1, 1, index, ALL, SERIAL[sender],
                                           bytes(8), rows))


def health(state, now):
    return {node["name"]: node["slot_lateness"] for node in state.snapshot(now)["node_health"]}


def test_a_nodes_slot_lateness_is_the_median_of_what_the_others_report_about_it():
    state = DashboardState(ROOM, mode="auto")
    for k, sender in enumerate(NAMES):                     # everyone announces its index first
        state.push_broadcast(T0 + k * 0.001, broadcast(sender, 1))
    state.push_broadcast(T0 + 0.05, broadcast("n1", 2, {"n2": 10}))   # code 10: 288-320 us
    state.push_broadcast(T0 + 0.06, broadcast("n3", 2, {"n2": 12}))   # code 12: 352-384 us
    state.push_broadcast(T0 + 0.07, broadcast("n4", 2, {"n2": 31}))   # 960 us or more
    lateness = health(state, T0 + 0.1)
    assert lateness["n2"] == 368                            # the middle of 304, 368 and 960
    assert lateness["n5"] == "unknown"                      # nobody measured n5


def test_a_newer_report_from_the_same_receiver_replaces_its_older_one():
    state = DashboardState(ROOM, mode="auto")
    for k, sender in enumerate(NAMES):
        state.push_broadcast(T0 + k * 0.001, broadcast(sender, 1))
    state.push_broadcast(T0 + 0.05, broadcast("n1", 2, {"n2": 31}))
    state.push_broadcast(T0 + 0.10, broadcast("n1", 3, {"n2": 5}))
    assert health(state, T0 + 0.2)["n2"] == 144             # code 5: 128-160 us


def test_reports_older_than_two_seconds_no_longer_count():
    state = DashboardState(ROOM, mode="auto")
    for k, sender in enumerate(NAMES):
        state.push_broadcast(T0 + k * 0.001, broadcast(sender, 1))
    state.push_broadcast(T0 + 0.05, broadcast("n1", 2, {"n2": 10}))
    state.push_broadcast(T0 + 3.0, broadcast("n3", 50))     # no lateness about n2 any more
    assert health(state, T0 + 3.0)["n2"] == "unknown"
