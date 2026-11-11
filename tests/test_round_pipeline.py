"""The transceiver pipeline: (host time, round broadcast) in, dashboard payload out.

Round broadcasts are built by hand (with the encoder the golden fixtures pin) for the frozen 8-node transceiver
room, fed to DashboardState, and every assertion is on its payload.
"""
from pathlib import Path

import numpy as np
import pytest

from csi.broadcast import FLAG_WARMING, FORMAT_VERSION, LinkReport, RoundBroadcast, encode_broadcast, quantise_score
from csi.dashboard import DashboardState
from csi.round_auth import TEST_KIT_KEY, sign_broadcast
from csi.room import load_room

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NAMES = [f"n{k}" for k in range(1, 9)]
SERIAL = {name: f"02:00:00:00:{k:02X}:5C" for k, name in enumerate(NAMES, 1)}
ALL = (1 << len(NAMES)) - 1
ROUND_HZ = 20.0
T0 = 1_700_000_000.0
QUIET = 2.0


def broadcast(sender, round_no, reports=None, *, membership=ALL, node_id=None, schedule=1):
    """Bytes of ``sender``'s round broadcast. ``reports`` maps transmitter name to LinkReport fields to override."""
    index = NAMES.index(sender)
    rows = []
    for i, name in enumerate(NAMES):
        if i == index or not membership >> i & 1:
            continue
        fields = {"score_q": quantise_score(QUIET), "rssi": -50, "heard": 15, "since": 0, "flags": 0}
        fields.update((reports or {}).get(name, {}))
        rows.append(LinkReport(i, **fields))
    return sign_broadcast(encode_broadcast(RoundBroadcast(FORMAT_VERSION, 1, round_no % 65536, index, len(NAMES) + 1,
                                                          schedule, index, membership, node_id or SERIAL[sender],
                                                          bytes(8), tuple(rows))), TEST_KIT_KEY)


def run(state, seconds, start_s=0.0, reports=lambda round_no, sender: None, senders=NAMES, seed=0):
    """Every sender broadcasts once per round in its slot; quiet links carry a slightly noisy quiet score."""
    rng = np.random.default_rng(seed)
    first = int(round(start_s * ROUND_HZ))
    for round_no in range(first, first + int(round(seconds * ROUND_HZ))):
        for slot, sender in enumerate(NAMES):
            if sender not in senders:
                continue
            quiet = {name: {"score_q": quantise_score(QUIET * rng.lognormal(0, 0.2))} for name in NAMES}
            quiet.update(reports(round_no, sender) or {})
            host_ts = T0 + round_no / ROUND_HZ + slot / (ROUND_HZ * (len(NAMES) + 1))
            state.push_broadcast(host_ts, broadcast(sender, round_no, quiet))
    return state.snapshot()


def links(payload):
    return {row["id"]: row for row in payload["links"]}


def auto_state():
    return DashboardState(ROOM, mode="auto")


def test_every_ordered_pair_of_transceivers_is_a_link_named_by_its_nodes():
    payload = auto_state().snapshot(T0)
    expected = {f"{rx}<-{tx}" for rx in NAMES for tx in NAMES if rx != tx}
    assert set(links(payload)) == expected and len(expected) == 56   # every serial ends in 5C: no collisions
    row = links(payload)["n3<-n7"]
    assert (row["receiver"], row["transmitter"], row["state"]) == ("n3", "n7", "NO_SIGNAL")
    assert sorted((n["name"], n["role"]) for n in payload["nodes"]) == [(n, "transceiver") for n in NAMES]


def test_quiet_reports_warm_up_then_read_empty():
    state = auto_state()
    early = run(state, 3.0)
    assert {row["state"] for row in early["links"]} == {"WARMUP"}
    late = run(state, 10.0, start_s=3.0)
    assert {row["state"] for row in late["links"]} == {"EMPTY"}
    assert late["status"]["room"]["state"] == "EMPTY"


HOT = {"score_q": quantise_score(40 * QUIET)}


def test_a_score_rising_on_links_reads_motion_on_those_links_only():
    state = auto_state()
    run(state, 12.0)
    hot = {"n1": {"n5": HOT}, "n5": {"n1": HOT}}
    payload = run(state, 2.0, start_s=12.0, reports=lambda round_no, sender: hot.get(sender))
    states = {label: row["state"] for label, row in links(payload).items()}
    assert states.pop("n1<-n5") == "MOTION" and states.pop("n5<-n1") == "MOTION"
    assert set(states.values()) == {"EMPTY"}
    # both directions of one node pair are one witness, not two (csi/room_state.py): the room is not raised
    assert payload["status"]["room"]["state"] == "EMPTY"


def test_links_from_different_nodes_rising_where_they_cross_raise_the_room_state():
    state = auto_state()
    run(state, 12.0)
    hot = {"n1": {"n5": HOT}, "n3": {"n7": HOT}, "n2": {"n6": HOT}, "n4": {"n8": HOT}}   # all cross the centre
    payload = run(state, 2.0, start_s=12.0, reports=lambda round_no, sender: hot.get(sender))
    room = payload["status"]["room"]
    assert room["state"] == "MOTION"
    assert room["elevated"] == ["n1<-n5", "n2<-n6", "n3<-n7", "n4<-n8"]


def test_a_link_its_receiver_no_longer_hears_reads_no_signal_from_the_heard_counter():
    state = auto_state()
    run(state, 12.0)
    deaf = {"heard": 0, "since": 15, "rssi": None, "score_q": 0}
    payload = run(state, 1.0, start_s=12.0, reports=lambda round_no, sender: {"n4": deaf} if sender == "n2" else None)
    row = links(payload)["n2<-n4"]
    assert row["state"] == "NO_SIGNAL" and row["age_s"] < 0.2   # its report is fresh: the counter says it
    assert row["pkt_rate"] == 0.0
    assert links(payload)["n4<-n2"]["state"] == "EMPTY"


def test_a_warming_flag_reads_warmup():
    state = auto_state()
    run(state, 12.0)
    warming = {"flags": FLAG_WARMING}
    payload = run(state, 1.0, start_s=12.0, reports=lambda round_no, sender: {"n6": warming} if sender == "n8" else None)
    assert links(payload)["n8<-n6"]["state"] == "WARMUP"
    assert links(payload)["n6<-n8"]["state"] == "EMPTY"


def test_a_silent_node_turns_its_received_links_no_signal_after_two_seconds():
    state = auto_state()
    run(state, 12.0)
    payload = run(state, 3.0, start_s=12.0, senders=[n for n in NAMES if n != "n2"])
    rows = links(payload)
    assert {rows[f"n2<-{tx}"]["state"] for tx in NAMES if tx != "n2"} == {"NO_SIGNAL"}


def test_link_rate_is_the_round_rate_scaled_by_the_heard_counter():
    state = auto_state()
    half = {"heard": 7, "since": 1}
    payload = run(state, 3.0, reports=lambda round_no, sender: {"n1": half} if sender == "n3" else None)
    assert links(payload)["n3<-n1"]["pkt_rate"] == pytest.approx(ROUND_HZ * 7 / 15, rel=0.1)
    assert links(payload)["n3<-n2"]["pkt_rate"] == pytest.approx(ROUND_HZ, rel=0.1)
    assert links(payload)["n3<-n1"]["rssi"] == -50


def test_rejected_bytes_and_unknown_nodes_are_counted_not_dropped_silently():
    state = auto_state()
    state.push_broadcast(T0, b"\x03" + broadcast("n1", 0)[1:])                       # a future version
    state.push_broadcast(T0, broadcast("n1", 0)[:-1])                                # truncated
    state.push_broadcast(T0, broadcast("n1", 0, node_id="02:00:00:00:99:5C"))         # not in the room config
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert counts["rejected"]["version"] == counts["rejected"]["length"] == 1
    assert sum(counts["rejected"].values()) == 2
    assert counts["unknown_nodes"] == {"02:00:00:00:99:5C": 1}
    assert counts["accepted"] == 0


def test_reports_about_a_node_not_yet_heard_are_counted_until_its_own_broadcast_places_it():
    state = auto_state()
    state.push_broadcast(T0, broadcast("n1", 0))         # n2..n8 have not sent: their index is not yet known
    counts = state.snapshot(T0)["status"]["broadcasts"]
    assert counts["accepted"] == 1 and counts["unplaced_reports"] == 7
    state.push_broadcast(T0 + 0.01, broadcast("n2", 0))  # n2 places index 1 (and its report on n1 is placed)
    state.push_broadcast(T0 + 0.05, broadcast("n1", 1))
    assert links(state.snapshot(T0 + 0.05))["n1<-n2"]["state"] == "WARMUP"


def test_transceiver_links_are_judged_by_their_adaptive_baseline_even_in_fixed_mode():
    state = DashboardState(ROOM, {"n1<-n2": 5.0})            # fixed mode, with a threshold given for one link
    payload = run(state, 13.0)
    rows = links(payload)
    assert {row["mode"] for row in rows.values()} == {"auto"}
    assert {row["state"] for row in rows.values()} == {"EMPTY"}   # none UNCALIBRATED: no calibration is needed
    assert rows["n1<-n2"]["threshold"] != 5.0


def test_a_threshold_for_a_transceiver_link_is_refused():
    state = auto_state()
    run(state, 12.0)
    with pytest.raises(ValueError, match="adaptive baseline"):
        state.set_threshold("n1<-n2", 5.0)
    assert state.thresholds == {}
    state.set_threshold("n1<-n2", None)                       # back to auto: it already is, nothing changes
    assert links(run(state, 1.0, start_s=12.0))["n1<-n2"]["mode"] == "auto"


def test_the_payload_names_the_zones_and_which_of_them_read_motion():
    state = auto_state()
    run(state, 12.0)
    hot = {"n1": {"n5": HOT}, "n3": {"n7": HOT}, "n2": {"n6": HOT}, "n4": {"n8": HOT}}   # all cross at (2, 1.5)
    payload = run(state, 1.0, start_s=12.0, reports=lambda round_no, sender: hot.get(sender))
    zones = payload["room"]["zones"]
    assert len(zones) == 12                                    # 4 x 3 m in 1 m zones: A1 top-left .. D3
    assert zones[0] == {"id": "A1", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}
    assert {z["id"] for z in zones} == {f"{c}{r}" for c in "ABCD" for r in (1, 2, 3)}
    states = payload["status"]["room"]["zones"]
    assert {zone for zone, s in states.items() if s == "MOTION"} == {"B2", "C2"}   # either side of the crossing
    assert {s for zone, s in states.items() if zone not in ("B2", "C2")} == {"EMPTY"}
    later = run(state, 1.5, start_s=13.0)        # the links fall back; the zones hold MOTION 1 s, then possibly occupied
    assert {zone for zone, s in later["status"]["room"]["zones"].items() if s != "EMPTY"} == {"B2", "C2"}
    assert set(later["status"]["room"]["zones"].values()) == {"EMPTY", "POSSIBLY_OCCUPIED"}
    assert later["status"]["room"]["state"] == "POSSIBLY_OCCUPIED"
    assert later["status"]["room"]["possibly_occupied_s"] == 60.0     # the default hold


def test_zones_that_stop_moving_read_possibly_occupied_for_the_set_hold_then_empty():
    state = DashboardState(ROOM, mode="auto", possibly_occupied_s=5.0)
    run(state, 12.0)
    hot = {"n1": {"n5": HOT}, "n3": {"n7": HOT}, "n2": {"n6": HOT}, "n4": {"n8": HOT}}
    run(state, 1.0, start_s=12.0, reports=lambda round_no, sender: hot.get(sender))
    held = run(state, 4.0, start_s=13.0)
    room = held["status"]["room"]
    assert (room["state"], room["possibly_occupied_s"]) == ("POSSIBLY_OCCUPIED", 5.0)
    assert {zone for zone, s in room["zones"].items() if s == "POSSIBLY_OCCUPIED"} == {"B2", "C2"}
    cleared = run(state, 3.0, start_s=17.0)
    assert cleared["status"]["room"]["state"] == "EMPTY"
    assert set(cleared["status"]["room"]["zones"].values()) == {"EMPTY"}
