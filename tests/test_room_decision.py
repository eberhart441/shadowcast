"""The room decision at scale (csi/adaptive.py RoomDecision): k of n, node diversity, a common point, the glitch veto,
the small-rig rules and persistence.

Most cases feed synthetic link readings straight into RoomDecision, so each one is exact: no packets, no scorer.
Labels follow csi/links.py LinkId.label: ``rx2<-01`` is receiver rx2 and the sender whose MAC ends in 01.
"""
import json
from pathlib import Path

from csi.adaptive import (EMPTY, MOTION, ROOM_ENTER_S, ROOM_HOLD_S, LinkPlace, Reading, RoomDecision,
                          required_links)
from csi.links import LinkId, LinkMonitor
from csi.room import load_room
from csi.simulate import simulate_room_links

RIG4 = Path(__file__).resolve().parent / "fixtures" / "room_rig4.json"   # the real 2 x 2 rig, 2.77 x 1.84 m
STEP = 0.05   # seconds between updates (live, the decision runs on every packet)


def labels(n_rx, n_tx):
    """Every link of a rig with receivers rx1, rx2, ... and senders 00, 01, ..."""
    return [f"rx{r}<-{s:02d}" for r in range(1, n_rx + 1) for s in range(n_tx)]


def readings(links, t, up=(), strong=(), cold=()):
    """Each link's reading at ``t``: elevated if in ``up``, strong if in ``strong``, still warming up if in ``cold``."""
    return {label: Reading(t, MOTION if label in up else EMPTY, 1.0, elevated=label in up,
                           strong=label in strong, warm=label not in cold)
            for label in links}


def run(decision, links, up, t0=0.0, seconds=2.0, **kw):
    """Feed the same elevated links every STEP for ``seconds`` from ``t0``: [(t, room state)]."""
    out = []
    for i in range(int(round(seconds / STEP))):
        t = t0 + i * STEP
        out.append((t, decision.update(t, readings(links, t, up, **kw))))
    return out


def final(decision, links, up, **kw):
    return run(decision, links, up, **kw)[-1][1]


def seen(decision, links, up, **kw):
    return {state for _, state in run(decision, links, up, **kw)}


def crossing_rig():
    """16 links over an 8 x 6 m section: receivers rx1..rx4 on the left wall and senders tx0..tx3 on the right wall,
    at the same four heights, so rxN<-0M runs from (0, Y[N-1]) to (8, Y[M])."""
    ys = (0.75, 2.25, 3.75, 5.25)
    places = {f"rx{r + 1}<-{s:02d}": LinkPlace(f"rx{r + 1}", f"tx{s}", ((0.0, ys[r]), (8.0, ys[s])))
              for r in range(4) for s in range(4)}
    return list(places), {"places": places, "bounds": (0.0, 0.0, 8.0, 6.0)}


def rig4():
    """The real rig's four links with their segments, as LinkMonitor builds them from a room."""
    room = load_room(RIG4)
    places = {LinkId(rx.name, tx.mac).label: LinkPlace(rx.name, tx.name, ((rx.x, rx.y), (tx.x, tx.y)))
              for rx, tx in room.links()}
    return list(places), {"places": places, "bounds": (0.0, 0.0, room.width_m, room.depth_m)}


# ---------------------------------------------------------------- k of n


def test_the_number_of_links_needed_grows_with_the_live_links():
    assert [required_links(n) for n in (1, 2, 4, 13)] == [2, 2, 2, 2]
    assert [required_links(n) for n in (14, 16, 20)] == [3, 3, 3]
    assert [required_links(n) for n in (21, 30)] == [4, 5]
    assert required_links(16, fraction=0.25) == 4


def test_two_links_are_enough_for_eight_links_but_not_for_sixteen():
    pair = {"rx1<-00", "rx2<-01"}                       # different receivers and senders
    assert final(RoomDecision(), labels(2, 4), pair) == MOTION
    decision = RoomDecision()
    assert MOTION not in seen(decision, labels(4, 4), pair)
    assert (decision.active, decision.required, decision.reason) == (16, 3, "too_few")
    assert final(RoomDecision(), labels(4, 4), pair | {"rx3<-02"}) == MOTION


def test_links_still_warming_up_do_not_raise_the_number_needed():
    sixteen = labels(4, 4)
    cold = {label for label in sixteen if label.startswith("rx4")} | {"rx3<-02", "rx3<-03"}   # 10 live links
    decision = RoomDecision()
    assert final(decision, sixteen, {"rx1<-00", "rx2<-01"}, cold=cold) == MOTION
    assert (decision.active, decision.required) == (10, 2)


# ---------------------------------------------------------------- node diversity


def test_links_of_one_receiver_alone_do_not_make_the_room_motion():
    links = labels(3, 6)                                # 18 links: 3 needed; rx1 has 6, so 3 of them is no glitch
    decision = RoomDecision()
    assert MOTION not in seen(decision, links, {"rx1<-00", "rx1<-01", "rx1<-02"})
    assert decision.reason == "diversity" and decision.glitch_nodes == []
    assert final(RoomDecision(), links, {"rx1<-00", "rx1<-01", "rx2<-02"}) == MOTION


def test_links_of_one_sender_alone_do_not_make_the_room_motion():
    links = labels(6, 3)                                # 18 links: 3 needed; sender 00 has 6
    decision = RoomDecision()
    assert MOTION not in seen(decision, links, {"rx1<-00", "rx2<-00", "rx3<-00"})
    assert decision.reason == "diversity" and decision.glitch_nodes == []
    assert final(RoomDecision(), links, {"rx1<-00", "rx2<-00", "rx3<-01"}) == MOTION


# ---------------------------------------------------------------- a common point


def test_links_that_cross_near_one_point_make_the_room_motion_and_report_the_point():
    links, geometry = crossing_rig()
    crossing = {"rx1<-03", "rx4<-00", "rx2<-02"}        # all three pass through (4, 3)
    decision = RoomDecision(**geometry)
    assert final(decision, links, crossing) == MOTION
    support = decision.support
    assert abs(support["x"] - 4.0) <= 0.3 and abs(support["y"] - 3.0) <= 0.3
    assert support["links"] == sorted(crossing) and support["radius_m"] == 1.0


def test_elevated_links_far_apart_leave_a_big_rig_empty():
    links, geometry = crossing_rig()
    # three receivers and three senders, but no point is within 1 m of all three (level at y = 0.75, 3.75, 5.25)
    decision = RoomDecision(**geometry)
    assert MOTION not in seen(decision, links, {"rx1<-00", "rx3<-02", "rx4<-03"})
    assert decision.reason == "spatial" and decision.support is None
    # two links far apart, at 16 links: too few, wherever they are
    assert MOTION not in seen(RoomDecision(**geometry), links, {"rx1<-00", "rx4<-03"})


def test_the_place_test_refuses_two_far_links_even_when_two_are_enough():
    links, geometry = crossing_rig()
    far = RoomDecision(fraction=0.0, **geometry)        # 2 needed at 16 links: only the place test can refuse
    assert MOTION not in seen(far, links, {"rx1<-00", "rx4<-03"})          # y = 0.75 and y = 5.25
    assert far.reason == "spatial"
    assert final(RoomDecision(fraction=0.0, **geometry), links, {"rx1<-03", "rx4<-00"}) == MOTION   # cross at (4, 3)


# ---------------------------------------------------------------- receiver- or sender-wide glitch


def test_a_receiver_wide_glitch_is_set_aside():
    sixteen = labels(4, 4)
    glitch = {"rx1<-00", "rx1<-01", "rx1<-02", "rx1<-03"}     # every link of rx1 at once (gain change, reset)
    noise = {"rx2<-01", "rx3<-02"}                            # 2 of the other 12 links
    decision = RoomDecision()
    assert MOTION not in seen(decision, sixteen, glitch | noise)
    assert decision.glitch_nodes == ["rx1"] and decision.counted == sorted(noise)
    # counted without the veto, the same links would have made the room MOTION
    assert final(RoomDecision(glitch_min_links=99), sixteen, glitch | noise) == MOTION
    # movement elsewhere too (more than 30% of the other links): no veto
    busy = glitch | noise | {"rx4<-03", "rx2<-03"}
    decision = RoomDecision()
    assert final(decision, sixteen, busy) == MOTION and decision.glitch_nodes == []


def test_a_sender_wide_glitch_is_set_aside_and_named():
    decision = RoomDecision()
    run(decision, labels(4, 4), {"rx1<-02", "rx2<-02", "rx3<-02", "rx4<-02", "rx1<-00"})
    assert decision.state == EMPTY and decision.glitch_nodes == ["02"]


# ---------------------------------------------------------------- small rigs


def test_one_strong_link_is_enough_only_on_a_small_rig():
    assert final(RoomDecision(), labels(2, 2), {"rx1<-00"}, strong={"rx1<-00"}) == MOTION
    decision = RoomDecision()
    assert MOTION not in seen(decision, labels(3, 3), {"rx1<-00"}, strong={"rx1<-00"})
    assert decision.reason == "too_few"


def test_any_two_links_agree_on_the_four_link_rig():
    # node diversity would leave only the two diagonals and the two walls of the 2 x 2 rig: it starts above 4 links
    for pair in ({"rx1<-00", "rx1<-01"}, {"rx1<-00", "rx2<-00"}, {"rx1<-00", "rx2<-01"}):
        assert final(RoomDecision(), labels(2, 2), pair) == MOTION, pair


# The default simulated walk (left wall, top, right wall, bottom, 0.5 m/s) disturbs the four links in turn: the left
# wall, one diagonal, the other diagonal, the right wall, the first diagonal again, ... Each link stays elevated
# about 3 s, and the next one rises about 2 s after the previous one did.
WALK = ("rx2<-01", "rx2<-00", "rx1<-01", "rx1<-00", "rx2<-00", "rx1<-01", "rx2<-01")


def walking(t):
    """Links elevated at walk time ``t``: link i of WALK is up over [2 i, 2 i + 3.2)."""
    return {label for i, label in enumerate(WALK) if 2.0 * i <= t < 2.0 * i + 3.2}


def test_the_four_link_rig_sees_a_normal_walk_as_before_but_half_a_second_later():
    links, geometry = rig4()
    new = RoomDecision(**geometry)
    old = RoomDecision(enter_s=0.0)                     # the old rule: two links, no wait, no geometry
    differ = []
    for i in range(int(round(16.0 / STEP))):
        t = i * STEP
        a, b = new.update(t, readings(links, t, walking(t))), old.update(t, readings(links, t, walking(t)))
        if a != b:
            differ.append((t, a, b))
    # the only difference is the wait before the first MOTION; the pairs that share a node meet at that node
    assert differ, "the new rule waits ROOM_ENTER_S before entering MOTION"
    assert all(2.0 <= t <= 2.0 + ROOM_ENTER_S + 0.01 and (a, b) == (EMPTY, MOTION) for t, a, b in differ), differ
    assert new.state == old.state == EMPTY


# ---------------------------------------------------------------- persistence


def test_links_must_agree_for_enter_s_and_motion_is_held_for_hold_s_after():
    small, pair = labels(2, 2), {"rx1<-00", "rx2<-01"}
    decision = RoomDecision()
    run(decision, small, set(), seconds=5.0)
    blip = run(decision, small, pair, t0=5.0, seconds=ROOM_ENTER_S - 0.2)    # shorter than ROOM_ENTER_S
    after = run(decision, small, set(), t0=5.0 + ROOM_ENTER_S - 0.2, seconds=2.0)
    assert {s for _, s in blip + after} == {EMPTY}
    on = run(decision, small, pair, t0=10.0, seconds=2.0)
    assert {s for t, s in on if t < 10.0 + ROOM_ENTER_S - 0.01} == {EMPTY}
    assert {s for t, s in on if t > 10.0 + ROOM_ENTER_S + 0.01} == {MOTION}
    off = run(decision, small, set(), t0=12.0, seconds=2.0)
    assert {s for t, s in off if t < 12.0 + ROOM_HOLD_S - 0.01} == {MOTION}
    assert {s for t, s in off if t > 12.0 + ROOM_HOLD_S + 0.01} == {EMPTY}


def test_agreement_back_within_the_hold_keeps_motion_without_a_new_wait():
    small, pair = labels(2, 2), {"rx1<-00", "rx2<-01"}
    decision = RoomDecision()
    run(decision, small, set(), seconds=5.0)
    assert final(decision, small, pair, t0=5.0) == MOTION
    gap = run(decision, small, set(), t0=7.0, seconds=ROOM_HOLD_S / 2)
    back = run(decision, small, pair, t0=7.0 + ROOM_HOLD_S / 2, seconds=1.0)
    assert {s for _, s in gap + back} == {MOTION}


# ---------------------------------------------------------------- LinkMonitor


def test_the_link_monitor_reports_the_room_decision_before_any_packet():
    status = LinkMonitor({}, room=load_room(RIG4)).room_status()
    assert status == {"state": "WARMUP", "elevated": [], "active": 0, "required": 2, "room_support": None,
                      "glitch_nodes": [], "reason": "warmup"}


def test_the_link_monitor_uses_the_room_geometry_for_the_support_point_in_fixed_mode():
    room = load_room(RIG4)
    links = {LinkId(rx.name, tx.mac).label for rx, tx in room.links()}
    thresholds = {label: 1e-9 for label in links}       # every scored packet is over it: all four links in MOTION
    packets = list(simulate_room_links(room=room, person=None, duration_s=4.0, seed=3))
    with_room, without = LinkMonitor(thresholds, room=room), LinkMonitor(thresholds)
    for receiver, pkt in packets:
        with_room.push(receiver, pkt)
        without.push(receiver, pkt)
    assert with_room.room_state == without.room_state == MOTION
    status = with_room.room_status()
    json.dumps(status)
    support = status["room_support"]
    assert support is not None and len(support["links"]) >= 2 and set(support["links"]) <= links
    assert 0.0 <= support["x"] <= room.width_m and 0.0 <= support["y"] <= room.depth_m
    assert status["active"] == 4 and status["glitch_nodes"] == []
    assert without.room_status()["room_support"] is None   # no geometry, no point
