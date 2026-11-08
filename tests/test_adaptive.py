"""Adaptive per-link detector and room decision, driven by (time, score) series. No packets, no hardware."""
import math
import random

from csi.adaptive import FREEZE_CAP_S, WARMUP_S, AdaptiveDetector, RoomDecision

HZ = 25.0  # samples per second, roughly the real packet rate


def series(values, t0=0.0, hz=HZ):
    return [(t0 + i / hz, v) for i, v in enumerate(values)]


def noisy(level, n, spread, seed=1):
    rng = random.Random(seed)
    return [max(0.01, rng.gauss(level, spread)) for _ in range(n)]


def states(det, pts):
    return [det.push(t, s).state for t, s in pts]


def test_warm_up_reports_warmup_then_quiet_noise_is_empty():
    pts = series(noisy(5.0, 25 * 40, 0.5))
    det = AdaptiveDetector()
    out = states(det, pts)
    assert out[0] == "WARMUP"
    assert all(s == "WARMUP" for (t, _), s in zip(pts, out) if t < 10.0)
    assert all(s == "EMPTY" for (t, _), s in zip(pts, out) if t >= 12.0)


def test_isolated_one_or_two_sample_spikes_do_not_trigger():
    values = noisy(5.0, 25 * 40, 0.5)
    for i in range(300, len(values), 200):
        values[i] = 60.0
        values[i + 1] = 60.0
    det = AdaptiveDetector()
    out = states(det, series(values))
    assert "MOTION" not in out


def elevated_runs(out, pts):
    """(start, end) host times of each run of MOTION in the state list ``out`` (aligned with ``pts``)."""
    runs, start = [], None
    for (t, _), s in zip(pts, out):
        if s == "MOTION" and start is None:
            start = t
        if s != "MOTION" and start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, pts[-1][0]))
    return runs


def test_scale_jump_after_a_scorer_restart_is_relearned_after_a_new_warm_up():
    # a receiver reboots (and re-rolls its gain): its scorer restarts its window, which shows as missing scores,
    # then the scale is 4x. The detector starts over, warms up on the new level and stays quiet.
    restart = [(30.0 + i / HZ, None) for i in range(46)]                  # the scorer's ~1.8 s refill
    pts = series(noisy(3.0, 25 * 30, 0.4, seed=2)) + restart + series(noisy(12.0, 25 * 60, 1.6, seed=3), t0=31.84)
    out = states(AdaptiveDetector(), pts)
    assert "MOTION" not in out
    assert {s for (t, _), s in zip(pts, out) if 30.0 <= t < 31.84 + WARMUP_S} == {"WARMUP"}
    late = [s for (t, _), s in zip(pts, out) if t > 31.84 + WARMUP_S + 2.0]
    assert late and set(late) == {"EMPTY"}


def test_scale_jump_without_a_restart_is_held_as_motion_until_the_freeze_cap_then_relearned():
    # a level that rises and stays, with no restart, looks like someone moving: the baseline is frozen while the
    # link is elevated, so it stays MOTION; after FREEZE_CAP_S the level is taken as the new normal
    pts = series(noisy(3.0, 25 * 30, 0.4, seed=2) + noisy(12.0, int(25 * (FREEZE_CAP_S + 40)), 1.6, seed=3))
    out = states(AdaptiveDetector(), pts)
    runs = elevated_runs(out, pts)
    assert len(runs) == 1, runs
    (start, end), = runs
    assert 30.0 <= start <= 31.5 and FREEZE_CAP_S + 29.0 <= end <= FREEZE_CAP_S + 33.0, runs
    late = [s for (t, _), s in zip(pts, out) if t > FREEZE_CAP_S + 35.0]
    assert late and set(late) == {"EMPTY"}


def test_scale_drop_is_relearned_and_then_stays_quiet():
    pts = series(noisy(12.0, 25 * 30, 1.6, seed=4) + noisy(3.0, 25 * 60, 0.4, seed=5))
    out = states(AdaptiveDetector(), pts)
    late = [s for (t, _), s in zip(pts, out) if t > 30.0 + 35.0]
    assert late and set(late) == {"EMPTY"}


def test_person_walking_elevates_the_link_and_it_leaves_afterwards():
    pts = series(noisy(4.0, 25 * 30, 0.4, seed=6) + noisy(12.0, 25 * 8, 1.0, seed=7)
                 + noisy(4.0, 25 * 30, 0.4, seed=8))
    runs = elevated_runs(states(AdaptiveDetector(), pts), pts)
    assert len(runs) == 1, runs
    (start, end), = runs
    assert 29.5 <= start <= 31.5 and 36.0 <= end <= 39.5, runs


def test_burst_shorter_than_min_dur_does_not_elevate():
    values = noisy(4.0, 25 * 30, 0.4, seed=9)
    for i in range(25 * 15, 25 * 15 + 8):  # 0.32 s at 3x: shorter than min_dur
        values[i] = 13.0
    assert "MOTION" not in states(AdaptiveDetector(), series(values))


def test_hysteresis_holds_elevated_on_a_level_between_exit_and_enter():
    # scores 4 +- 0.2: baseline ~3.9 (median of the lower 60%), enter ~5.85 (the 1.5x floor), exit ~4.9 (the
    # 1.25x floor). Walk at 12, then sit at 5.35 (between): still elevated, and the frozen baseline does not move.
    pts = series(noisy(4.0, 25 * 20, 0.2, seed=10) + noisy(12.0, 25 * 2, 0.2, seed=11)
                 + noisy(5.35, 25 * 6, 0.1, seed=12))
    out = states(AdaptiveDetector(), pts)
    between = [s for (t, _), s in zip(pts, out) if 23.0 <= t < 27.5]
    assert between and set(between) == {"MOTION"}


def test_a_lift_under_twice_the_quiet_level_is_detected():
    # a walker lifted the real rig's links to only 1.8-3x their quiet level, below the old 2x floor
    pts = series(noisy(4.0, 25 * 40, 0.4, seed=16) + noisy(7.2, 25 * 10, 0.4, seed=17)
                 + noisy(4.0, 25 * 20, 0.4, seed=18))
    runs = elevated_runs(states(AdaptiveDetector(), pts), pts)
    assert len(runs) == 1, runs
    (start, end), = runs
    assert 40.0 <= start <= 41.5 and 49.5 <= end <= 51.0, runs


def test_someone_moving_for_minutes_stays_detected_because_the_baseline_is_frozen():
    # four minutes of movement: the old detector learned a level that filled most of 30 s as the new normal
    pts = series(noisy(4.0, 25 * 30, 0.4, seed=19) + noisy(12.0, 25 * 240, 1.0, seed=20)
                 + noisy(4.0, 25 * 30, 0.4, seed=21))
    out = states(AdaptiveDetector(), pts)
    runs = elevated_runs(out, pts)
    assert len(runs) == 1, runs
    (start, end), = runs
    assert 30.0 <= start <= 31.5 and 269.5 <= end <= 271.0, runs
    assert {s for (t, _), s in zip(pts, out) if t > 272.0} == {"EMPTY"}   # the quiet baseline was kept


def test_a_score_gap_after_valid_scores_restarts_the_warm_up():
    det = AdaptiveDetector()
    for t, s in series(noisy(4.0, 25 * 20, 0.4, seed=22)):
        det.push(t, s)
    assert det.push(20.0, None).state == "WARMUP"
    assert det.push(20.5, 4.0).state == "WARMUP"
    assert det.push(20.5 + WARMUP_S - 1.0, 4.0).state == "WARMUP"


def test_one_noisy_link_alone_does_not_make_the_room_motion():
    room = RoomDecision()
    noisy_det, calm_det = AdaptiveDetector(), AdaptiveDetector()
    seen = set()
    for i in range(int(40 * HZ)):
        t = i / HZ
        spike = 10.0 if 20.0 <= t <= 22.0 else 4.0   # one link elevated (2.5x) for 2 s: not strong, so no room
        readings = {"rx1<-00": noisy_det.push(t, spike), "rx2<-00": calm_det.push(t, 4.0 + 0.1 * math.sin(t))}
        seen.add(room.update(t, readings))
    assert "MOTION" not in seen


def test_two_links_elevated_together_make_the_room_motion():
    room = RoomDecision()
    dets = {"rx1<-00": AdaptiveDetector(), "rx2<-01": AdaptiveDetector()}
    seen = set()
    for i in range(int(40 * HZ)):
        t = i / HZ
        walking = 20.0 <= t <= 28.0
        readings = {label: dets[label].push(t, (12.0 if walking else 4.0) + 0.3 * math.sin(3 * t + k))
                    for k, label in enumerate(dets)}
        seen.add(room.update(t, readings))
    assert "MOTION" in seen
