"""Dashboard state payload and heatmap, driven by simulated (receiver, packet) streams through the text seam.

Assertions are on the payload only (``DashboardState.snapshot``), never on internal weights.
"""
import dataclasses
import json
from functools import lru_cache

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.links import LinkMonitor, calibrate_links_report
from csi.room import load_room, stand_at_centre, stand_near_wall
from csi.simulate import simulate_room_links

T0 = 1_700_000_000.0
ROOM = load_room()
LEFT = "rx2<-01"    # rx2 (0, 0) to tx1 (0, 1.84): the left wall
RIGHT = "rx1<-00"   # rx1 (2.77, 0) to tx0 (2.77, 1.84): the right wall
DIAG_A = "rx2<-00"  # rx2 (0, 0) to tx0 (2.77, 1.84)
DIAG_B = "rx1<-01"  # rx1 (2.77, 0) to tx1 (0, 1.84): down-weighted in the committed room config
LABELS = {LEFT, RIGHT, DIAG_A, DIAG_B}


def tagged(**kwargs):
    return list(simulate_room_links(**kwargs))


@lru_cache(maxsize=None)
def calibrated_thresholds():
    """Thresholds from a simulated empty room, the calibration the dashboard would be given."""
    # margin 1.5 (not the default 1.0): at 1.0 the held-out empty room crosses the single-clip p99 (see test_calibration_margin.py)
    return calibrate_links_report(tagged(person=None, duration_s=25, seed=10), margin=1.5).thresholds


LEAD_S = 12.0  # empty room before a person arrives, so every link learns its quiet baseline first


def quiet_then(person, duration_s, seed=12, drop_after=None):
    """An empty room for LEAD_S seconds, then ``person`` for ``duration_s`` (both on the same clock)."""
    return tagged(person=None, duration_s=LEAD_S, seed=seed) + tagged(
        person=person, duration_s=duration_s, seed=seed, t0=T0 + LEAD_S, drop_after=drop_after)


def feed(state, packets, until=None):
    """Push packets (optionally only those before host time ``until``) into the dashboard state."""
    for receiver, pkt in packets:
        if until is not None and pkt.host_ts >= until:
            break
        state.push(receiver, pkt)


def new_state(thresholds=None):
    return DashboardState(ROOM, calibrated_thresholds() if thresholds is None else thresholds)


def run(packets, thresholds=None):
    state = new_state(thresholds)
    feed(state, packets)
    return state


def links_by_id(payload):
    return {row["id"]: row for row in payload["links"]}


def grid(payload):
    heat = payload["heatmap"]
    return np.array(heat["cells"]).reshape(heat["rows"], heat["cols"])


def test_payload_is_json_serialisable_and_has_the_documented_top_level_keys():
    payload = run(tagged(person=None, duration_s=3, seed=1)).snapshot()
    json.dumps(payload)
    assert {"room", "nodes", "links", "heatmap", "status"} <= set(payload)
    assert payload["room"] == {"width_m": 2.77, "depth_m": 1.84}
    assert {row["id"] for row in payload["links"]} == LABELS
    heat = payload["heatmap"]
    assert len(heat["cells"]) == heat["cols"] * heat["rows"]


def test_nodes_come_from_the_room_config_with_positions_and_chips():
    nodes = {n["name"]: n for n in run(tagged(person=None, duration_s=3, seed=1)).snapshot()["nodes"]}
    assert set(nodes) == {"rx1", "rx2", "tx0", "tx1"}
    assert nodes["rx1"] == {"name": "rx1", "role": "receiver", "chip": "ESP32-S3", "x": 2.77, "y": 0.0}
    assert nodes["tx1"]["role"] == "sender" and nodes["tx1"]["x"] == 0.0 and nodes["tx1"]["y"] == 1.84


def test_empty_room_is_cold_with_every_link_quiet_and_none_contributing():
    payload = run(tagged(person=None, duration_s=15, seed=11)).snapshot()
    assert payload["heatmap"]["max"] < 0.02
    assert payload["heatmap"]["contributing"] == []
    assert payload["status"]["links_by_state"]["EMPTY"] == 4
    assert not any(row["contributes"] for row in payload["links"])


def test_each_link_reports_its_sender_node_weight_and_normalised_score():
    payload = run(tagged(person=None, duration_s=5, seed=11)).snapshot()
    rows = links_by_id(payload)
    assert rows[RIGHT]["sender"] == "tx0" and rows[RIGHT]["receiver"] == "rx1"
    assert rows[LEFT]["sender"] == "tx1"
    assert rows[DIAG_B]["weight"] == 0.3
    assert rows[RIGHT]["weight"] == 1.0
    assert rows[RIGHT]["normalised"] is not None and rows[RIGHT]["normalised"] < 1.0
    assert rows[RIGHT]["threshold"] > 0 and rows[RIGHT]["mac"].endswith("00")


def test_person_near_right_wall_puts_the_hottest_cells_on_the_right_half():
    payload = run(quiet_then(stand_near_wall(ROOM, "right"), 10)).snapshot()
    g = grid(payload)
    half = g.shape[1] // 2
    left_max, right_max = g[:, :half].max(), g[:, half:].max()
    assert right_max > 0.3
    assert left_max < 0.5 * right_max
    _, hottest_col = np.unravel_index(np.argmax(g), g.shape)
    assert hottest_col >= half
    assert RIGHT in payload["heatmap"]["contributing"]
    assert LEFT not in payload["heatmap"]["contributing"]


def test_person_near_left_wall_puts_the_hottest_cells_on_the_left_half():
    payload = run(quiet_then(stand_near_wall(ROOM, "left"), 10)).snapshot()
    g = grid(payload)
    half = g.shape[1] // 2
    left_max, right_max = g[:, :half].max(), g[:, half:].max()
    assert left_max > 0.3
    assert right_max < 0.5 * left_max
    _, hottest_col = np.unravel_index(np.argmax(g), g.shape)
    assert hottest_col < half


def test_person_at_centre_is_hot_driven_by_both_diagonals_and_hotter_than_corners():
    payload = run(quiet_then(stand_at_centre(ROOM), 10, seed=13)).snapshot()
    assert set(payload["heatmap"]["contributing"]) == {DIAG_A, DIAG_B}
    g = grid(payload)
    rows, cols = g.shape
    centre = g[rows // 2 - 1:rows // 2 + 1, cols // 2 - 1:cols // 2 + 1].max()
    corners = [g[0, 0], g[0, -1], g[-1, 0], g[-1, -1]]
    assert centre > 0.3
    assert centre > max(corners)


@pytest.mark.slow
def test_link_that_drops_out_stops_contributing_and_its_heat_vanishes():
    packets = quiet_then(stand_near_wall(ROOM, "left"), 10, seed=16, drop_after={LEFT: 3.0})  # drops at T0+15
    state = new_state()
    feed(state, packets, until=T0 + LEAD_S + 2.5)
    before = state.snapshot(now=T0 + LEAD_S + 2.5)
    assert LEFT in before["heatmap"]["contributing"]
    assert before["heatmap"]["max"] > 0.3

    feed(state, packets)
    after = state.snapshot(now=T0 + LEAD_S + 10.0)
    row = links_by_id(after)[LEFT]
    assert row["state"] == "NO_SIGNAL"
    assert row["contributes"] is False
    assert LEFT not in after["heatmap"]["contributing"]
    assert after["heatmap"]["max"] < 0.05


def test_uncalibrated_link_is_reported_as_such_and_contributes_no_heat():
    thresholds = {k: v for k, v in calibrated_thresholds().items() if k != RIGHT}
    payload = run(tagged(person=stand_near_wall(ROOM, "right"), duration_s=10, seed=12), thresholds).snapshot()
    row = links_by_id(payload)[RIGHT]
    assert row["state"] == "UNCALIBRATED"
    assert row["threshold"] is None
    assert row["normalised"] is None
    assert row["contributes"] is False
    assert RIGHT not in payload["heatmap"]["contributing"]
    assert payload["heatmap"]["max"] < 0.05


def test_after_the_person_leaves_the_heatmap_decays_back_to_cold_within_seconds():
    state = new_state()
    lead = tagged(person=None, duration_s=LEAD_S, seed=12)
    standing = tagged(person=stand_near_wall(ROOM, "right"), duration_s=6, seed=12, t0=T0 + LEAD_S)
    left_at = T0 + LEAD_S + 6.0
    empty = tagged(person=None, duration_s=10, seed=12, t0=left_at)
    feed(state, lead + standing)
    assert state.snapshot(now=left_at)["heatmap"]["max"] > 0.3
    maxima = []
    for seconds_after in (0.2, 0.5, 1.0, 2.0, 5.0):   # default decay time constant is 0.3 s
        feed(state, empty, until=left_at + seconds_after)
        maxima.append(state.snapshot(now=left_at + seconds_after)["heatmap"]["max"])
    assert maxima[0] > 0.1                      # fades over time, it does not switch off
    assert all(a > b for a, b in zip(maxima, maxima[1:]))
    assert maxima[-1] < 0.05                    # cold five seconds after the person left


def test_packets_from_an_unknown_mac_are_reported_but_never_drawn():
    packets = tagged(person=stand_near_wall(ROOM, "right"), duration_s=6, seed=12)
    state = new_state()
    for receiver, pkt in packets:
        state.push(receiver, dataclasses.replace(pkt, mac="1a:00:00:00:00:07") if pkt.mac.endswith("00") else pkt)
    payload = state.snapshot()
    unknown = [row for row in payload["links"] if row["id"] == "rx1<-07"]
    assert unknown and unknown[0]["sender"] is None
    assert unknown[0]["contributes"] is False
    assert "rx1<-07" not in payload["heatmap"]["contributing"]


def test_each_link_exposes_its_evidence_and_quiet_baseline_and_the_model_parameters():
    payload = run(quiet_then(stand_near_wall(ROOM, "right"), 10)).snapshot()
    rows = links_by_id(payload)
    for label in LABELS:
        assert 0.0 <= rows[label]["evidence"] <= 1.0
        assert rows[label]["baseline"] is not None and rows[label]["baseline"] > 0
    assert rows[RIGHT]["evidence"] > 0.3
    assert rows[RIGHT]["evidence"] >= rows[LEFT]["evidence"]
    params = payload["status"]["parameters"]
    for key in ("r_floor", "r_full", "baseline_window_s", "baseline_percentile", "baseline_warmup_s",
                "sensitivity", "agreement"):
        assert key in params
    assert "reference_ratio" not in params
    json.dumps(payload)


def test_baseline_is_none_during_warm_up_and_evidence_zero_until_ten_seconds_of_history():
    early = run(tagged(person=stand_near_wall(ROOM, "right"), duration_s=6, seed=12)).snapshot()
    rows = links_by_id(early)
    assert all(rows[label]["baseline"] is None for label in LABELS)
    assert all(rows[label]["evidence"] == 0.0 for label in LABELS)
    assert early["heatmap"]["max"] == 0.0


# ---------------------------------------------------------------- link health, near-node veto, last motion

HEALTH = {"seq_rate": 38.46, "seq_loss": 0.0625, "clip_pct": 0.025, "agc_gain": 32, "fft_gain": -6,
          "noise_floor": -92}


def test_link_health_from_the_monitor_rows_reaches_each_link_and_is_none_where_a_row_has_none(monkeypatch):
    """The monitor's rows (csi/links.py) carry seq_rate, seq_loss, clip_pct, agc_gain, fft_gain and noise_floor; the
    payload passes them through per link. A row without them (an older monitor) gives None, so the page and old
    logs still work, and a NaN never reaches the JSON."""
    original = LinkMonitor.snapshot

    def rows_with_health(self, now=None):
        rows = [{k: v for k, v in row.items() if k not in HEALTH} for row in original(self, now)]
        for row in rows:
            if row["id"] == RIGHT:
                row.update(HEALTH)
            elif row["id"] == DIAG_A:
                row.update(seq_rate=float("nan"), seq_loss=None, agc_gain=float("inf"))
        return rows

    monkeypatch.setattr(LinkMonitor, "snapshot", rows_with_health)
    payload = run(tagged(person=None, duration_s=3, seed=1)).snapshot()
    json.dumps(payload, allow_nan=False)
    rows = links_by_id(payload)
    assert {key: rows[RIGHT][key] for key in HEALTH} == HEALTH
    assert isinstance(rows[RIGHT]["agc_gain"], int) and isinstance(rows[RIGHT]["noise_floor"], int)
    assert all(rows[LEFT][key] is None for key in HEALTH)          # the row had none of them
    assert all(rows[DIAG_A][key] is None for key in HEALTH)        # NaN, inf and None all become None


def test_payload_reports_no_busy_node_on_the_four_link_rig_and_keeps_its_old_keys():
    payload = run(quiet_then(stand_near_wall(ROOM, "right"), 6)).snapshot()
    assert payload["heatmap"]["busy_nodes"] == []                  # every node has two links: never busy
    assert {"cols", "rows", "extents", "cells", "max", "contributing"} <= set(payload["heatmap"])
    params = payload["status"]["parameters"]
    assert {"coverage_eps", "min_coverage", "baseline_freeze_s", "busy_evidence", "busy_link_weight"} <= set(params)


def test_links_and_the_room_report_seconds_since_their_last_motion():
    state = new_state()
    standing = tagged(person=stand_near_wall(ROOM, "right"), duration_s=6, seed=12, t0=T0 + LEAD_S)
    left_at = T0 + LEAD_S + 6.0
    empty = tagged(person=None, duration_s=10, seed=12, t0=left_at)
    feed(state, tagged(person=None, duration_s=LEAD_S, seed=12))
    quiet = state.snapshot(now=T0 + LEAD_S)
    assert quiet["status"]["room"]["since_motion_s"] is None      # no MOTION yet
    assert all(row["since_motion_s"] is None for row in quiet["links"])

    feed(state, standing)
    during = state.snapshot(now=left_at)
    rows = links_by_id(during)
    assert rows[RIGHT]["state"] == "MOTION" and rows[RIGHT]["since_motion_s"] == 0.0
    assert during["status"]["room"]["since_motion_s"] == 0.0
    assert rows[LEFT]["since_motion_s"] is None

    feed(state, empty)
    after = state.snapshot(now=left_at + 10.0)
    since = links_by_id(after)[RIGHT]["since_motion_s"]
    assert 5.0 < since <= 10.0              # its MOTION ended a second or two after the person left
    assert after["status"]["room"]["since_motion_s"] == since


def test_heatmap_payload_names_the_estimator_that_drew_the_frame():
    """heatmap.estimator (added with csi/rti.py) is the estimator of the frame and status.parameters.estimator the
    configured one: the four-link rig under the default auto draws with the back-projection, and a forced RTI says so
    in both places while the image keeps its format."""
    auto = run(quiet_then(stand_near_wall(ROOM, "right"), 4)).snapshot()
    assert auto["heatmap"]["estimator"] == "backprojection"
    assert auto["status"]["parameters"]["estimator"] == "auto"

    state = DashboardState(ROOM, calibrated_thresholds(), estimator="rti")
    feed(state, quiet_then(stand_near_wall(ROOM, "right"), 4))
    forced = state.snapshot()
    heat = forced["heatmap"]
    assert heat["estimator"] == "rti"
    assert forced["status"]["parameters"]["estimator"] == "rti"
    assert len(heat["cells"]) == heat["cols"] * heat["rows"]
    assert all(0.0 <= v <= 1.0 for v in heat["cells"]) and heat["max"] == max(heat["cells"])
    json.dumps(forced, allow_nan=False)
