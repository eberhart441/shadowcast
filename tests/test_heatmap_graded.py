"""Graded heatmap evidence: each link is judged against its own adaptive quiet baseline, not its threshold.

The seam is ``Heatmap.advance(now, rows)`` fed with link snapshot rows (id, state, score, threshold,
age_s), so the real-rig regression can be written as an explicit score timeline. The end-to-end
payload checks are in tests/test_dashboard_state.py.
"""
import numpy as np

from csi.heatmap import Heatmap, link_evidence
from csi.room import load_room

ROOM = load_room()
RIGHT = "rx1<-00"
DIAG_A = "rx2<-00"
DIAG_B = "rx1<-01"
THR = 10.0  # arbitrary threshold: the evidence must not depend on it


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
    # One link alone is damped by agreement (0.5), so a lone 1.8x rise peaks near 0.17. That is about
    # 8x the empty-room floor (< 0.02 in test_noise_around_a_quiet_baseline_stays_cold), so it is visible; a walk seen by
    # two links reaches about 0.7 on the real timeline (see the validation numbers in the report).
    assert peak(heat) > 0.12
    # the heat sits on the link's own footprint: the falloff-weighted cells are the hottest
    assert heat.values[np.argmax(heat._falloff[DIAG_A])] > 0.5 * peak(heat)
    assert last > 0


def test_weight_scales_a_links_evidence():
    # rx1<-01 is down-weighted to 0.3 in config/room.json; rx1<-00 has weight 1.0. Same score history.
    assert ROOM.link_weights["rx1<-01"] == 0.3
    down, full = Heatmap(ROOM), Heatmap(ROOM)
    replay(down, "rx1<-01", [1.0] * 44 + [5.0] * 40)
    replay(full, RIGHT, [1.0] * 44 + [5.0] * 40)
    assert 0.0 < peak(down) < 0.5 * peak(full)


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
