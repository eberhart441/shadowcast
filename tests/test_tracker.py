"""The one-person tracker on the heatmap image (csi/tracker.py), its payload in the dashboard state, and replays.

The tracker is driven through its seam, heatmap payloads in and the track payload out, with synthetic images of a
moving blob; the dashboard and replay tests go through DashboardState.snapshot and the replay server's events.
"""
import json
import math
import random

import pytest

from csi.tracker import (ACTIVITY_MIN, COAST_S, LOST_HOLD_S, MARGIN_M, MAX_SPEED, TRAIL_S, YOUNG_COAST_S, Tracker,
                         find_blobs)

W, D = 6.0, 4.0            # a floor section bigger than the rig, so walking speed and jumps have room
COLS, ROWS = 30, 20        # 0.2 m cells
FRAME_S = 0.2              # the server's frame interval (5 Hz)
T0 = 1_700_000_000.0
PAYLOAD_KEYS = {"active", "x", "y", "sx", "sy", "rho", "vx", "vy", "age_s", "last_seen_s", "trail"}


def image(*blobs, links=("rx1<-00", "rx2<-01", "rx2<-00"), w=W, d=D, cols=COLS, rows=ROWS):
    """A heatmap payload with Gaussian blobs (x, y, peak, sigma_x[, sigma_y]); row 0 is y = 0, as the dashboard's."""
    cells = []
    for r in range(rows):
        for c in range(cols):
            x, y = (c + 0.5) * w / cols, (r + 0.5) * d / rows
            v = 0.0
            for blob in blobs:
                bx, by, peak, sx = blob[:4]
                sy = blob[4] if len(blob) > 4 else sx
                v = max(v, peak * math.exp(-(x - bx) ** 2 / (2 * sx ** 2) - (y - by) ** 2 / (2 * sy ** 2)))
            cells.append(round(v, 4))
    return {"cols": cols, "rows": rows, "extents": {"x_min": 0.0, "y_min": 0.0, "x_max": w, "y_max": d},
            "cells": cells, "max": max(cells), "contributing": list(links), "busy_nodes": []}


def quiet():
    return image()


def person(x, y, peak=0.5, sigma=0.4):
    return image((x, y, peak, sigma))


def walk(tracker, path, times, jitter=0.0, seed=1):
    """Feed one frame per time with a blob at path(t) (plus a little jitter); return [(t, (x, y), track)]."""
    rng = random.Random(seed)
    out = []
    for t in times:
        x, y = path(t)
        payload = tracker.update(T0 + t, person(x + rng.uniform(-jitter, jitter), y + rng.uniform(-jitter, jitter)))
        out.append((t, (x, y), payload))
    return out


def frames(start, stop, step=FRAME_S):
    n = int(round((stop - start) / step))
    return [start + k * step for k in range(n)]


def across(t):
    """Walking across the section at 0.8 m/s, left to right at mid depth."""
    return 0.6 + 0.8 * t, 2.0


def error(track, pos):
    return math.hypot(track["x"] - pos[0], track["y"] - pos[1])


# ---------------------------------------------------------------- measurement


def test_a_single_blob_is_measured_at_its_centre_by_its_weighted_centroid():
    [blob] = find_blobs(person(2.3, 1.7))
    assert blob.x == pytest.approx(2.3, abs=0.05) and blob.y == pytest.approx(1.7, abs=0.05)
    assert blob.var_x >= 0.2 ** 2 and blob.var_y >= 0.2 ** 2      # never tighter than one 0.2 m cell


def test_a_blob_drawn_out_along_one_axis_measures_wider_along_it():
    [blob] = find_blobs(image((3.0, 2.0, 0.5, 1.5, 0.25)))           # long in x, narrow in y: like one link
    assert blob.var_x > 4 * blob.var_y


def test_two_separate_blobs_are_two_measurements_strongest_first_each_at_its_own_centre():
    blobs = find_blobs(image((1.0, 1.0, 0.4, 0.35), (4.5, 3.0, 0.7, 0.35)))
    assert len(blobs) == 2
    assert (blobs[0].x, blobs[0].y) == (pytest.approx(4.5, abs=0.1), pytest.approx(3.0, abs=0.1))
    assert (blobs[1].x, blobs[1].y) == (pytest.approx(1.0, abs=0.1), pytest.approx(1.0, abs=0.1))
    assert blobs[0].peak > blobs[1].peak


def test_a_quiet_image_or_one_contributing_link_gives_no_measurement():
    assert find_blobs(quiet()) == []
    assert find_blobs(image((3.0, 2.0, ACTIVITY_MIN * 0.8, 0.4))) == []        # below the activity level
    assert find_blobs(image((3.0, 2.0, 0.6, 0.4), links=("rx1<-00",))) == []  # one link cannot place a person


@pytest.mark.parametrize("heatmap", [None, {}, {"cols": 2, "rows": 2, "cells": [0.5]}, "cells",
                                     {"cols": 2, "rows": 1, "cells": [0.9, 0.9], "extents": {"x_min": 0}},
                                     {"cols": 2, "rows": 1, "cells": [float("nan"), None],
                                      "extents": {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1}}])
def test_a_missing_or_malformed_heatmap_gives_no_measurement_and_no_error(heatmap):
    assert find_blobs(heatmap) == []
    tracker = Tracker()
    assert tracker.update(T0, heatmap) is None
    assert tracker.update(T0 + 1, heatmap) is None


# ---------------------------------------------------------------- birth and following


def test_a_track_is_born_on_the_third_consistent_frame():
    tracker = Tracker()
    payloads = [tracker.update(T0 + t, person(3.0, 2.0)) for t in frames(0, 1.0)]
    assert payloads[0] is None and payloads[1] is None
    born = payloads[2]
    assert born["active"] is True
    assert born["age_s"] == pytest.approx(2 * FRAME_S) and born["last_seen_s"] == 0
    assert len(born["trail"]) == 3
    assert all(p["active"] for p in payloads[2:])


def test_payload_is_json_with_the_contract_keys_in_metres_and_seconds():
    tracker = Tracker()
    for t in frames(0, 2.0):
        payload = tracker.update(T0 + t, person(*across(t)))
    assert PAYLOAD_KEYS <= set(payload)
    json.dumps(payload, allow_nan=False)
    assert -1 <= payload["rho"] <= 1 and payload["sx"] > 0 and payload["sy"] > 0
    for point in payload["trail"]:
        assert len(point) == 3 and T0 <= point[0] <= T0 + 2.0      # host time, then metres


def test_a_walker_crossing_the_section_is_followed_closely_with_its_velocity():
    run = walk(Tracker(), across, frames(0, 6.0), jitter=0.1)
    settled = [(pos, track) for t, pos, track in run if t >= 1.5]
    assert all(track is not None and track["active"] for _, track in settled)
    errors = sorted(error(track, pos) for pos, track in settled)
    assert errors[len(errors) // 2] < 0.2                 # median, in 0.2 m cells
    assert errors[-1] < 0.4
    final = run[-1][2]
    assert final["vx"] == pytest.approx(0.8, abs=0.25) and final["vy"] == pytest.approx(0.0, abs=0.25)


def test_irregular_frame_times_are_handled():
    rng = random.Random(3)
    times, t = [], 0.0
    while t < 6.0:
        times.append(t)
        t += rng.uniform(0.1, 0.6)                        # 0.1 s to 0.6 s between frames
    run = walk(Tracker(), across, times, jitter=0.05)
    settled = [error(track, pos) for t, pos, track in run if t >= 2.0]
    assert settled and max(settled) < 0.45


def test_a_call_between_frames_does_not_step_the_filter():
    tracker = Tracker()
    for t in frames(0, 2.0):
        payload = tracker.update(T0 + t, person(*across(t)))
    again = tracker.update(T0 + 1.8 + 0.05, person(5.5, 3.5))  # 50 ms later (an /api/state request): no step
    assert {k: again[k] for k in ("x", "y", "vx", "vy")} == {k: payload[k] for k in ("x", "y", "vx", "vy")}


def test_speed_estimates_never_exceed_the_walking_limit():
    run = walk(Tracker(), lambda t: (0.5 + 3.0 * t, 2.0), frames(0, 1.8))   # 3 m/s, faster than anyone walks
    speeds = [math.hypot(track["vx"], track["vy"]) for _, _, track in run if track]
    assert speeds and max(speeds) <= MAX_SPEED + 1e-3                    # 1e-3: the payload rounds to mm/s


# ---------------------------------------------------------------- gating


def test_a_one_frame_jump_across_the_room_is_gated_out():
    tracker = Tracker()
    for t in frames(0, 3.0):
        tracker.update(T0 + t, person(*across(t)))
    jumped = tracker.update(T0 + 3.0, person(5.5, 0.4))             # 3 m away in 0.2 s
    assert jumped["active"] and jumped["last_seen_s"] > 0              # coasting: not measured this frame
    assert error(jumped, across(3.0)) < 0.4
    back = tracker.update(T0 + 3.2, person(*across(3.2)))
    assert back["last_seen_s"] == 0 and error(back, across(3.2)) < 0.4


def test_the_track_keeps_its_person_when_a_stronger_blob_appears_elsewhere():
    tracker = Tracker()
    for t in frames(0, 3.0):
        tracker.update(T0 + t, person(*across(t)))
    for t in frames(3.0, 4.0):
        x, y = across(t)
        payload = tracker.update(T0 + t, image((x, y, 0.4, 0.4), (5.3, 0.5, 0.8, 0.4)))
        assert payload["last_seen_s"] == 0 and error(payload, (x, y)) < 0.4


# ---------------------------------------------------------------- coasting, last seen, drop


def follow_then_stop(seconds=4.0):
    """A tracker that followed a walker for ``seconds`` and then saw nothing; returns it and the last position."""
    tracker = Tracker()
    for t in frames(0, seconds):
        tracker.update(T0 + t, person(*across(t)))
    return tracker, across(seconds - FRAME_S)


def test_a_lost_track_coasts_then_becomes_a_last_seen_marker_then_disappears():
    tracker, _ = follow_then_stop()
    last_measured = tracker.payload(T0 + 3.8)
    coasting = [tracker.update(T0 + 3.8 + k * FRAME_S, quiet()) for k in range(1, 15)]   # 0.2 s to 2.8 s unseen
    assert all(p["active"] for p in coasting)
    seen = [p["last_seen_s"] for p in coasting]
    assert seen == sorted(seen) and seen[-1] == pytest.approx(2.8, abs=0.01) and 2.8 < COAST_S
    marker = tracker.update(T0 + 3.8 + COAST_S + 0.1, quiet())
    assert marker["active"] is False
    assert (marker["x"], marker["y"]) == (last_measured["x"], last_measured["y"])   # where it was last measured
    assert marker["vx"] == 0 and marker["vy"] == 0
    later = tracker.update(T0 + 3.8 + 60.0, quiet())
    assert later["active"] is False and later["last_seen_s"] == pytest.approx(60.0)
    assert later["sx"] > marker["sx"] and later["sy"] > marker["sy"]   # the uncertainty grows while unseen
    assert later["trail"] == []                                         # older than the 20 s trail
    assert tracker.update(T0 + 3.8 + LOST_HOLD_S + 1.0, quiet()) is None


def test_a_short_lived_track_vanishes_without_a_marker():
    tracker = Tracker()
    for t in frames(0, 0.8):                                            # 4 frames: born, but young
        assert tracker.update(T0 + t, person(3.0, 2.0)) is None or t >= 0.4
    after = [tracker.update(T0 + t, quiet()) for t in frames(0.8, 0.6 + YOUNG_COAST_S + 1.0)]
    assert after[0] is not None and after[0]["active"]                  # coasts a little
    assert after[-1] is None                                            # then is gone, with no 'last seen'


def test_a_new_track_is_shown_instead_of_the_last_seen_marker():
    tracker, _ = follow_then_stop()
    tracker.update(T0 + 10.0, quiet())
    assert tracker.payload(T0 + 10.0)["active"] is False
    for t in frames(20.0, 21.0):
        payload = tracker.update(T0 + t, person(1.0, 1.0))
    assert payload["active"] is True and error(payload, (1.0, 1.0)) < 0.3
    assert payload["age_s"] < 1.0


def test_a_noise_burst_after_a_loss_does_not_erase_the_last_seen_marker():
    tracker, _ = follow_then_stop()
    marker = tracker.update(T0 + 10.0, quiet())
    for t in frames(12.0, 12.8):                                        # 4 frames of a blob elsewhere: young
        tracker.update(T0 + t, person(1.0, 3.5))
    assert tracker.payload(T0 + 12.6)["active"] is True                 # shown while it lasts
    after = tracker.update(T0 + 15.0, quiet())
    assert after["active"] is False and (after["x"], after["y"]) == (marker["x"], marker["y"])


def test_a_person_picked_up_elsewhere_takes_over_a_coasting_track():
    tracker = Tracker()
    for t in frames(0, 3.0):
        tracker.update(T0 + t, person(*across(t)))
    for t in frames(3.0, 5.0):                                           # now only seen 3 m away, for 2 s
        payload = tracker.update(T0 + t, person(5.4, 0.5))
    assert payload["active"] and payload["last_seen_s"] == 0 and error(payload, (5.4, 0.5)) < 0.3


def test_a_coasting_estimate_stays_inside_the_room_plus_the_margin():
    tracker = Tracker()
    for t in frames(0, 2.6):
        tracker.update(T0 + t, person(2.8 + 1.2 * t, 2.0))              # heading for the right wall at 1.2 m/s
    xs = [tracker.update(T0 + t, quiet())["x"] for t in frames(2.6, 5.0)]   # then unseen, coasting on
    assert W < max(xs) <= W + MARGIN_M + 1e-9


def test_a_clock_that_jumps_back_restarts_the_tracker():
    tracker, _ = follow_then_stop()
    assert tracker.update(T0 - 100.0, person(3.0, 2.0)) is None          # a new run's clock: start over


def test_the_trail_keeps_the_last_20_seconds_oldest_first():
    tracker = Tracker()
    for t in frames(0, 40.0):
        payload = tracker.update(T0 + t, person(3.0 + 2.0 * math.cos(t / 3), 2.0 + 1.5 * math.sin(t / 3)))
    times = [point[0] for point in payload["trail"]]
    assert times == sorted(times)
    assert times[0] >= T0 + 39.8 - TRAIL_S - 0.01 and times[-1] == pytest.approx(T0 + 39.8, abs=0.01)
    assert 80 <= len(times) <= 101                                     # about one point per frame


# ---------------------------------------------------------------- dashboard state and replays


def snapshots(state, packets, every_s=FRAME_S):
    """Push packets into a DashboardState and take a snapshot every ``every_s`` of packet time, as the server does."""
    out, next_at = [], None
    for receiver, pkt in packets:
        state.push(receiver, pkt)
        if next_at is None:
            next_at = pkt.host_ts + every_s
        elif pkt.host_ts >= next_at:
            out.append(state.snapshot(pkt.host_ts))
            next_at = pkt.host_ts + every_s
    return out


def test_dashboard_payload_has_a_null_track_in_an_empty_room():
    from tests.test_dashboard_state import new_state, tagged

    payloads = snapshots(new_state(), tagged(person=None, duration_s=15, seed=11))
    assert payloads and all("track" in p and p["track"] is None for p in payloads)
    json.dumps(payloads[-1], allow_nan=False)


def test_dashboard_follows_a_person_at_the_centre_of_the_rig():
    from csi.room import stand_at_centre
    from tests.test_dashboard_state import ROOM, new_state, quiet_then

    payloads = snapshots(new_state(), quiet_then(stand_at_centre(ROOM), 10, seed=13))
    track = payloads[-1]["track"]
    assert track is not None and track["active"] is True
    assert math.hypot(track["x"] - ROOM.width_m / 2, track["y"] - ROOM.depth_m / 2) < 0.4
    assert track["trail"] and track["trail"][-1][0] <= payloads[-1]["status"]["time_s"]
    json.dumps(payloads[-1], allow_nan=False)


def test_a_log_from_before_the_tracker_replays_with_a_track_computed_from_its_heatmaps(tmp_path):
    from csi.replay import ReplayServer, load_log
    from csi.server import LOG_FORMAT
    from tests.test_dashboard_server import read_events

    log = tmp_path / "old.jsonl"
    lines = [json.dumps({"format": LOG_FORMAT, "version": 1})]
    for t in frames(0, 4.0):                                       # an old payload: no track key at all
        lines.append(json.dumps({"links": [], "heatmap": person(*across(t)),
                                 "status": {"time_s": T0 + t, "finished": False}}))
    log.write_text("\n".join(lines) + "\n")
    server = ReplayServer(load_log(log), speed=math.inf, port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=30)
        events = read_events(server)
    finally:
        server.stop()
    assert len(events) == len(lines) - 1 and all("track" in e for e in events)
    final = events[-1]["track"]
    assert final["active"] is True and error(final, across(3.8)) < 0.4
