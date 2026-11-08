"""Graded heatmap evidence: each link is judged against its own adaptive quiet baseline, not its threshold.

The seam is ``Heatmap.advance(now, rows)`` fed with link snapshot rows (id, state, score, threshold,
age_s), so the real-rig regression can be written as an explicit score timeline. The end-to-end
payload checks are in tests/test_dashboard_state.py.

A link only counts towards the coverage normalisation once it reports (EMPTY or MOTION with a baseline), so
a test that feeds one link sees that link alone in the room.
"""
from pathlib import Path

import numpy as np

from csi.heatmap import Heatmap, cell_centres, link_evidence
from csi.room import load_room, point_segment_distance

ROOM = load_room()
ROOM5 = load_room(Path(__file__).resolve().parent / "fixtures" / "room_5node.json")  # + rx3 at (1.385, 0)
RIGHT = "rx1<-00"
DIAG_A = "rx2<-00"
DIAG_B = "rx1<-01"
CROSSING = (1.385, 0.92)       # where the two diagonals cross, the room centre
THR = 10.0  # arbitrary threshold: the evidence must not depend on it
TX0_LINKS = ("rx1<-00", "rx2<-00", "rx3<-00")   # in ROOM5 each sender has three links
TX1_LINKS = ("rx1<-01", "rx2<-01", "rx3<-01")


def row(label, score, state="EMPTY", threshold=THR, age_s=0.0):
    return {"id": label, "state": state, "score": score, "threshold": threshold, "age_s": age_s}


def replay(heat, label, scores, start=0.0, step=0.25, state="EMPTY"):
    """Feed one link's score series at host times start, start+step, ... and return the last time."""
    now = start
    for score in scores:
        heat.advance(now, [row(label, score, state=state)])
        now += step
    return now - step


def peak(heat):
    return float(np.max(heat.values))


def test_evidence_is_zero_at_or_below_the_dead_zone_and_full_at_r_full():
    baseline = 2.0
    assert link_evidence(2.0, baseline) == 0.0           # at baseline
    assert link_evidence(2.0 * 1.1, baseline) == 0.0     # noise inside the dead zone (r_floor 1.2)
    assert link_evidence(2.0 * 3.0, baseline) == 1.0     # r_full: full evidence
    assert link_evidence(2.0 * 30.0, baseline) == 1.0    # clipped


def test_evidence_grows_monotonically_between_the_dead_zone_and_r_full():
    baseline = 1.0
    values = [link_evidence(r, baseline) for r in np.linspace(1.0, 3.0, 25)]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(a <= b for a, b in zip(values, values[1:]))
    assert 0.0 < link_evidence(1.8, baseline) < 1.0


def test_evidence_is_zero_without_a_score_or_a_baseline():
    assert link_evidence(None, 1.0) == 0.0
    assert link_evidence(2.0, None) == 0.0
    assert link_evidence(2.0, 0.0) == 0.0


def test_no_link_contributes_before_it_has_ten_seconds_of_baseline_history():
    heat = Heatmap(ROOM)
    # 9 s of quiet, then a clear rise: still in warm-up, so nothing may light up
    scores = [1.0] * 36 + [4.0] * 4          # 0.25 s steps: 9 s quiet, 1 s loud
    replay(heat, RIGHT, scores)
    assert heat.contributing == []
    assert peak(heat) == 0.0


def test_a_link_contributes_once_its_baseline_has_ten_seconds_of_history():
    heat = Heatmap(ROOM)
    scores = [1.0] * 44 + [4.0] * 8          # 11 s quiet, then 2 s at 4x quiet
    replay(heat, RIGHT, scores)
    assert RIGHT in heat.contributing
    assert peak(heat) > 0.3


def test_noise_around_a_quiet_baseline_stays_cold():
    rng = np.random.default_rng(3)
    heat = Heatmap(ROOM)
    scores = 1.0 + 0.08 * rng.standard_normal(400)   # +-8 % jitter, inside the dead zone
    replay(heat, RIGHT, np.clip(scores, 0.5, None))
    assert heat.contributing == []
    assert peak(heat) < 0.02


def test_real_rig_modest_rise_below_threshold_still_forms_visible_heat():
    """Regression for the real four-node rig.

    On real data a walking person lifts a link from about 0.3 of its threshold to about 0.55 (1.8x its
    quiet level) and never crosses it. The old threshold-relative rule gave no heat here; the graded
    rule must show a clearly visible blob on that link's footprint.
    """
    heat = Heatmap(ROOM)
    quiet, walking = 0.3 * THR, 0.55 * THR
    assert walking < THR                      # never crosses the calibrated threshold
    last = replay(heat, DIAG_A, [quiet] * 44 + [walking] * 40)   # 11 s quiet, 10 s walking
    assert DIAG_A in heat.contributing
    # One link alone gets no agreement (0.5), so a lone 1.8x rise peaks near 0.22. That is about 10x the
    # empty-room floor (< 0.02 in test_noise_around_a_quiet_baseline_stays_cold), so it is visible; two links
    # that cross reach more (test_two_agreeing_links_make_a_cell_hotter_than_one_link_alone).
    assert peak(heat) > 0.12
    # the heat sits on the link's own footprint: the falloff-weighted cells are the hottest
    assert heat.values[np.argmax(heat._falloff[DIAG_A])] > 0.5 * peak(heat)
    assert last > 0


def test_weight_scales_a_links_evidence():
    # rx1<-01 is down-weighted to 0.3 in the committed room config; rx1<-00 has weight 1.0. Same score history.
    # Updated for the coverage normalisation: a cell is the weighted mean of the links that see it, so alone in
    # the room a link's weight only acts through eps (0.31 against 0.41 here, where the old sum gave less than
    # half). Where links overlap, the weight decides how much a link's evidence moves the cell: next block.
    assert ROOM.link_weights["rx1<-01"] == 0.3
    down, full = Heatmap(ROOM), Heatmap(ROOM)
    replay(down, "rx1<-01", [1.0] * 44 + [5.0] * 40)
    replay(full, RIGHT, [1.0] * 44 + [5.0] * 40)
    assert 0.0 < peak(down) < 0.85 * peak(full)

    # at the crossing of the two diagonals, the down-weighted one raised moves the cell far less than the other
    probe = Heatmap(ROOM)
    centre_cell = int(np.argmax(np.minimum(probe._falloff[DIAG_A], probe._falloff[DIAG_B])))
    a_raised, b_raised = Heatmap(ROOM), Heatmap(ROOM)
    replay_links(a_raised, {DIAG_A: [1.0] * 44 + [5.0] * 12, DIAG_B: [1.0] * 56})
    replay_links(b_raised, {DIAG_A: [1.0] * 56, DIAG_B: [1.0] * 44 + [5.0] * 12})
    assert 0.0 < b_raised.values[centre_cell] < 0.5 * a_raised.values[centre_cell]


def test_decay_to_cold_after_the_link_returns_to_quiet():
    heat = Heatmap(ROOM)
    t = replay(heat, RIGHT, [1.0] * 44 + [5.0] * 16)  # 11 s quiet, 4 s loud
    hot = peak(heat)
    assert hot > 0.3
    for k in range(1, 40):                            # 10 s back at quiet
        heat.advance(t + 0.25 * k, [row(RIGHT, 1.0)])
    assert peak(heat) < 0.05


def test_non_contributing_states_give_no_evidence():
    heat = Heatmap(ROOM)
    replay(heat, RIGHT, [1.0] * 44 + [5.0] * 40, state="NO_SIGNAL")
    assert heat.contributing == []
    assert peak(heat) == 0.0


def test_evidence_and_baseline_are_exposed_per_link():
    heat = Heatmap(ROOM)
    replay(heat, DIAG_A, [1.0] * 44 + [3.0] * 8)
    assert 0.9 <= heat.evidence[DIAG_A] <= 1.0
    assert heat.baselines[DIAG_A] == 1.0
    assert heat.evidence.get(RIGHT, 0.0) == 0.0


def replay_links(heat, series, start=0.0, step=0.25):
    """Feed several links' score series together, one snapshot per step (all links every step)."""
    length = min(len(s) for s in series.values())
    now = start
    for k in range(length):
        heat.advance(now, [row(label, s[k]) for label, s in series.items()])
        now += step


def test_two_agreeing_links_make_a_cell_hotter_than_one_link_alone():
    """The two diagonals cross at the room centre: both at full evidence there beat one of them alone."""
    probe = Heatmap(ROOM)
    centre_cell = int(np.argmax(np.minimum(probe._falloff[DIAG_A], probe._falloff[DIAG_B])))
    series = {DIAG_A: [1.0] * 44 + [3.0] * 40, DIAG_B: [1.0] * 44 + [3.0] * 40}
    one, both = Heatmap(ROOM), Heatmap(ROOM)
    replay(one, DIAG_A, series[DIAG_A])
    replay_links(both, series)
    assert one.values[centre_cell] > 0.2
    assert both.values[centre_cell] > one.values[centre_cell] + 0.1


def test_a_single_noisy_link_gives_less_heat_than_the_same_evidence_on_two_links():
    """Agreement suppresses lone spikes: one link at full evidence is cooler than two at the same cell."""
    lone, pair = Heatmap(ROOM), Heatmap(ROOM)
    replay(lone, DIAG_A, [1.0] * 44 + [3.0] * 8)
    replay_links(pair, {DIAG_A: [1.0] * 44 + [3.0] * 8, DIAG_B: [1.0] * 44 + [3.0] * 8})
    assert peak(lone) < peak(pair)
    assert peak(lone) < 0.9 * peak(pair)


def hottest_cell(heat, room=ROOM):
    centres = cell_centres(heat.cols, heat.rows, room.width_m, room.depth_m)
    return centres[int(np.argmax(heat.values))]


def distance_to_link(point, a, b):
    return float(point_segment_distance(np.asarray([point], dtype=float), a, b)[0])


def nearest_node(point, room=ROOM):
    return min(np.hypot(point[0] - n.x, point[1] - n.y) for n in room.receivers + room.senders)


# ---------------------------------------------------------------- coverage normalisation


def test_a_rise_on_every_link_at_once_is_not_drawn_at_a_node():
    """Multipath or a common-mode change lifts every link together. The old sum drew that at the rx2 corner,
    where two full-weight footprints meet; normalised, a corner is about as hot as one link alone, and the only
    extra heat is where two links cross."""
    heat = Heatmap(ROOM)
    replay_links(heat, {label: [1.0] * 44 + [1.8] * 12 for label in sorted(heat.known_labels)})
    hot = hottest_cell(heat)
    assert nearest_node(hot) > 0.5
    assert np.hypot(hot[0] - CROSSING[0], hot[1] - CROSSING[1]) < 0.2


def test_empty_room_noise_does_not_concentrate_at_the_nodes():
    """Independent ~1 s bursts on every link, as real empty rooms show. The old sum put most such false peaks
    within 0.5 m of a node, where the footprints meet; normalised, a lone burst paints its own link evenly and
    the quiet links that meet it at the nodes pull those cells down."""
    rng = np.random.default_rng(5)
    heat = Heatmap(ROOM)
    labels = sorted(heat.known_labels)
    centres = cell_centres(heat.cols, heat.rows, ROOM.width_m, ROOM.depth_m)
    burst = {label: 0 for label in labels}
    peaks = near_node = 0
    for k in range(800):                      # 0.1 s steps: 11 s of quiet history, then about 69 s of bursts
        t = 0.1 * k
        rows = []
        for label in labels:
            if t > 11.0:
                if burst[label]:
                    burst[label] -= 1
                elif rng.random() < 0.015:   # bursts of 1 s, about 13 % of the time
                    burst[label] = 10
            ratio = rng.uniform(1.5, 3.0) if burst[label] else 1.0 + 0.05 * rng.standard_normal()
            rows.append(row(label, ratio))
        heat.advance(t, rows)
        if t > 12.0 and peak(heat) >= 0.1:
            peaks += 1
            near_node += nearest_node(centres[int(np.argmax(heat.values))]) <= 0.5
    assert peaks > 50                         # the bursts do put heat on the map...
    assert near_node / peaks < 0.4            # ...but mostly along the links, not at their ends


def test_a_person_crossing_two_links_puts_the_hottest_cell_on_both_of_them():
    """Two links raised unequally (3x and 2x their quiet level), the walls quiet: the crossing still wins."""
    heat = Heatmap(ROOM)
    series = {label: [1.0] * 56 for label in sorted(heat.known_labels)}
    series[DIAG_A] = [1.0] * 44 + [3.0] * 12
    series[DIAG_B] = [1.0] * 44 + [2.0] * 12
    replay_links(heat, series)
    hot = hottest_cell(heat)
    assert distance_to_link(hot, (0.0, 0.0), (2.77, 1.84)) < 0.15     # on DIAG_A
    assert distance_to_link(hot, (2.77, 0.0), (0.0, 1.84)) < 0.15     # and on DIAG_B
    assert np.hypot(hot[0] - CROSSING[0], hot[1] - CROSSING[1]) < 0.2
    assert peak(heat) > 0.4                                          # clearly visible


def test_a_quiet_link_across_a_raised_one_pulls_the_cell_down_but_a_silent_link_does_not():
    """Coverage counts only links that report: a quiet one says 'not here', a silent one says nothing."""
    probe = Heatmap(ROOM)
    centre_cell = int(np.argmax(np.minimum(probe._falloff[DIAG_A], probe._falloff[DIAG_B])))
    quiet_a, silent_a = Heatmap(ROOM), Heatmap(ROOM)
    replay_links(quiet_a, {DIAG_A: [1.0] * 56, DIAG_B: [1.0] * 44 + [3.0] * 12})
    for k in range(56):                       # the same, but DIAG_A never reports (it has no signal)
        silent_a.advance(0.25 * k, [row(DIAG_A, 1.0, state="NO_SIGNAL"), row(DIAG_B, 1.0 if k < 44 else 3.0)])
    assert 0.0 < quiet_a.values[centre_cell] < 0.6 * silent_a.values[centre_cell]


# ---------------------------------------------------------------- the baseline learns from quiet only


def test_continuous_motion_stays_visible_and_the_link_reads_quiet_again_at_once():
    """A minute of movement keeps the link in MOTION: the baseline does not learn it, so the heat stays, and
    once the link is quiet again it is judged against its old quiet level straight away (not blinded)."""
    heat = Heatmap(ROOM)
    t = replay(heat, RIGHT, [1.0] * 80)                                      # 20 s quiet
    t = replay(heat, RIGHT, [3.0] * 240, start=t + 0.25, state="MOTION")     # 60 s of continuous movement
    assert heat.baselines[RIGHT] == 1.0
    assert heat.evidence[RIGHT] == 1.0
    assert peak(heat) > 0.3
    t = replay(heat, RIGHT, [1.0] * 8, start=t + 0.25)                       # quiet again
    assert heat.baselines[RIGHT] == 1.0 and heat.evidence[RIGHT] == 0.0
    replay(heat, RIGHT, [2.0] * 8, start=t + 0.25, state="MOTION")           # and the next walk reads at once
    assert heat.evidence[RIGHT] > 0.5


def test_a_level_held_longer_than_the_freeze_cap_is_learned_as_the_new_baseline():
    """A receiver that re-rolls its gain sits in MOTION at a new level: shown for up to 90 s, then learned."""
    heat = Heatmap(ROOM)
    t = replay(heat, RIGHT, [1.0] * 80)                                      # 20 s quiet
    t = replay(heat, RIGHT, [4.0] * 340, start=t + 0.25, state="MOTION")     # 85 s at 4x: still frozen
    assert heat.baselines[RIGHT] == 1.0 and heat.evidence[RIGHT] == 1.0
    replay(heat, RIGHT, [4.0] * 120, start=t + 0.25, state="MOTION")         # 30 s more: past the 90 s cap
    assert heat.baselines[RIGHT] == 4.0
    assert heat.evidence[RIGHT] == 0.0


# ---------------------------------------------------------------- near-node veto


def node_series(raised, ratio=3.0):
    """ROOM5 links: 11 s quiet, then 3 s with the ``raised`` links at ``ratio`` x quiet and the rest quiet."""
    return {label: [1.0] * 44 + [ratio if label in raised else 1.0] * 12 for label in TX0_LINKS + TX1_LINKS}


def test_movement_next_to_a_node_marks_it_busy_and_mutes_its_starburst():
    """All three tx0 links rise while the tx1 links stay quiet: someone is right next to tx0. Its links are
    down-weighted, so the fan of heat they would paint across the room is much fainter."""
    vetoed, unmuted = Heatmap(ROOM5), Heatmap(ROOM5, busy_link_weight=1.0)
    for heat in (vetoed, unmuted):
        replay_links(heat, node_series(TX0_LINKS))
        assert heat.busy_nodes == ["tx0"]
        assert set(heat.contributing) == set(TX0_LINKS)       # still raised and reported, only muted
    centres = cell_centres(vetoed.cols, vetoed.rows, ROOM5.width_m, ROOM5.depth_m)
    far = np.hypot(centres[:, 0] - 2.77, centres[:, 1] - 1.84) > 1.0   # more than 1 m from tx0
    assert vetoed.values[far].max() < 0.5 * unmuted.values[far].max()
    assert peak(vetoed) < 0.7 * peak(unmuted)


def test_links_of_several_nodes_rising_together_mark_no_node_busy():
    heat = Heatmap(ROOM5)
    replay_links(heat, node_series({"rx3<-00", "rx1<-01"}))   # a person crossing one link of each sender
    assert heat.busy_nodes == []
    everything = Heatmap(ROOM5)
    replay_links(everything, node_series(TX0_LINKS + TX1_LINKS))   # every link up: not one node's doing
    assert everything.busy_nodes == []


def test_a_node_needs_three_links_to_be_busy():
    heat = Heatmap(ROOM)                      # the four-link rig: every node has two links
    replay_links(heat, {label: [1.0] * 44 + ([3.0] * 12 if label in (RIGHT, DIAG_A) else [1.0] * 12)
                        for label in sorted(heat.known_labels)})   # both tx0 links up, the others quiet
    assert heat.busy_nodes == []


def test_a_busy_node_is_held_for_a_second_after_its_links_settle():
    heat = Heatmap(ROOM5)
    replay_links(heat, node_series(TX0_LINKS))   # last step at t = 13.75 s
    quiet = [row(label, 1.0) for label in TX0_LINKS + TX1_LINKS]
    heat.advance(13.75 + 0.5, quiet)
    assert heat.busy_nodes == ["tx0"]
    heat.advance(13.75 + 1.25, quiet)
    assert heat.busy_nodes == []
