"""scripts/eval_log.py: scoring a labelled dashboard log (detection, latency, false alarms, localisation, scaling).

The logs here are written in the dashboard's --log format: a header line, then one state payload per frame. Frames
come every 0.25 s from T0, which binary floating point holds exactly, so every label boundary falls on a known frame.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_log  # noqa: E402

T0 = 1_700_000_000.0
STEP = 0.25
A, B, C = "rx1<-00", "rx1<-01", "rx2<-00"


def link_row(label, state, level=None, **extra):
    """One link of a payload, with the keys csi/dashboard.py writes."""
    row = {"id": label, "receiver": label.split("<-")[0], "sender": None, "mac": "1a:0c:12:00:00:" + label[-2:],
           "state": state, "mode": "fixed", "elevated": state == "MOTION", "level": level, "score": None,
           "threshold": 1.0, "normalised": None, "pkt_rate": 38.0, "drops": 0, "rssi": -50, "age_s": 0.01,
           "baseline": None, "evidence": 0.0, "weight": 1.0, "contributes": False}
    row.update(extra)
    return row


def payload(t, links, room_state, hot=None, heat=0.9):
    """A payload at host time ``t``. The map is 4 x 2 cells over a 4 m x 2 m room; ``hot`` is the index of its one
    warm cell (row-major, row 0 at y = 0), or None for a cold map."""
    cells = [0.0] * 8
    if hot is not None:
        cells[hot] = heat
    return {"room": {"width_m": 4.0, "depth_m": 2.0}, "nodes": [], "links": links,
            "heatmap": {"cols": 4, "rows": 2, "extents": {"x_min": 0.0, "y_min": 0.0, "x_max": 4.0, "y_max": 2.0},
                        "cells": cells, "max": max(cells), "contributing": []},
            "status": {"time_s": t, "links_by_state": {}, "parameters": {},
                       "room": {"state": room_state, "elevated": [], "mode": "fixed"}, "finished": False}}


def header():
    return {"format": "csi-dashboard-log", "version": 1, "room": {}, "thresholds": {}, "mode": "fixed",
            "frame_interval_s": 0.2, "heatmap": {}, "notes": [], "source": "synthetic", "started": T0 - 2}


def write_log(path, frames, tail=""):
    path.write_text("\n".join(json.dumps(x) for x in [header(), *frames]) + "\n" + tail, encoding="utf-8")
    return path


def write_labels(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def score(tmp_path, frames, labels, **kwargs):
    """Write a log and a labels file, and evaluate them."""
    log = eval_log.read_log(write_log(tmp_path / "run.jsonl", frames))
    rows = eval_log.read_labels(write_labels(tmp_path / "labels.csv", labels), absolute=kwargs.get("absolute", False))
    return eval_log.evaluate(log, rows, **kwargs)


def walk_frames():
    """60 s. Link A is in MOTION 22-40 s; link B 25-38 s and again 50-51 s (a false alarm); the room when both are.
    The warm cell is inside the zone 0,0,2,1 for 22-32 s and outside it, at (3.5, 0.5), for 32-36 s; the map is cold
    otherwise. B's AGC gain steps from 30 to 34 at 30 s."""
    frames = []
    for i in range(240):
        rel = i * STEP
        a = "MOTION" if 22.0 <= rel < 40.0 else "EMPTY"
        b = "MOTION" if 25.0 <= rel < 38.0 or 50.0 <= rel < 51.0 else "EMPTY"
        hot = 0 if 22.0 <= rel < 32.0 else 3 if 32.0 <= rel < 36.0 else None
        links = [link_row(A, a, 1.5 if a == "MOTION" else 0.5, seq_rate=38.46, agc_gain=30),
                 link_row(B, b, 1.5 if b == "MOTION" else 0.5, seq_rate=38.46, agc_gain=30 if rel < 30 else 34,
                          rssi=-60)]
        frames.append(payload(T0 + rel, links, "MOTION" if a == b == "MOTION" else "EMPTY", hot))
    return frames


WALK_LABELS = 'start_s,end_s,label,zone\n0,20,empty,\n20,40,walking,"0,0,2,1"\n40,60,empty,\n'


@pytest.fixture
def walk(tmp_path):
    return score(tmp_path, walk_frames(), WALK_LABELS)


# ---------------------------------------------------------------------------------------------- statistics

def test_wilson_intervals_match_known_values():
    low, high = eval_log.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=1e-4)
    low, high = eval_log.wilson(10, 10)
    assert high == 1.0 and low == pytest.approx(0.7225, abs=1e-4)
    assert eval_log.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert eval_log.wilson(0, 0) is None


def test_no_false_alarm_in_an_hour_bounds_the_rate_at_about_3_8_per_hour():
    rate = eval_log.per_hour(0, 18000, 3600.0)
    assert rate["rate"] == 0.0 and rate["ci95"][0] == 0.0
    assert rate["ci95"][1] == pytest.approx(3.84, abs=0.01)


def test_link_subsets_are_all_taken_when_few_and_drawn_reproducibly_when_many():
    assert eval_log.link_subsets(4, 2, 50) == ([(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)], True)
    drawn, complete = eval_log.link_subsets(10, 5, 7, seed=0)
    assert not complete and len(set(drawn)) == 7 and drawn == sorted(drawn)
    assert all(len(combo) == 5 and list(combo) == sorted(combo) for combo in drawn)
    assert eval_log.link_subsets(10, 5, 7, seed=0) == (drawn, False)
    assert eval_log.link_subsets(10, 5, 7, seed=1)[0] != drawn


def test_votes_over_many_distinct_link_patterns_match_the_fast_path():
    motion = [i % 512 for i in range(2000)]
    decided = [(i * 7) % 512 | (i % 512) for i in range(2000)]
    assert len(set(zip(motion, decided))) > 256 >= len(set(zip(motion[:200], decided[:200])))  # both code paths
    many, few = eval_log._Votes(motion, decided), eval_log._Votes(motion[:200], decided[:200])
    for mask, k in ((0b1, 1), (0b1011, 2), (0x1FF, 3)):
        expected = bytes(2 if (m & mask).bit_count() >= k else 1 if d & mask else 0 for m, d in zip(motion, decided))
        assert many.codes(mask, k) == expected
        assert few.codes(mask, k) == expected[:200]


# ---------------------------------------------------------------------------------------------- scoring

def test_detection_frames_in_motion_and_latency_per_link_and_for_the_room(walk):
    s = walk["subjects"]
    assert walk["links"] == [A, B]
    a, b, room = s[A]["motion"], s[B]["motion"], s["room"]["motion"]
    assert (a["frames_in_motion"]["count"], a["frames_in_motion"]["total"]) == (72, 80)
    assert a["frames_in_motion"]["rate"] == pytest.approx(0.9)
    assert b["frames_in_motion"]["count"] == 52 and room["frames_in_motion"]["count"] == 52
    assert a["latency_s"]["median"] == pytest.approx(2.0)
    assert b["latency_s"]["median"] == pytest.approx(5.0) and room["latency_s"]["median"] == pytest.approx(5.0)
    for m in (a, b, room):
        assert (m["episodes_detected"]["count"], m["episodes_detected"]["total"]) == (1, 1)
        low, high = m["frames_in_motion"]["ci95"]
        assert low < m["frames_in_motion"]["rate"] < high
    assert a["level_median"] == 1.5 and s[A]["empty"]["level_median"] == 0.5
    assert walk["episodes"][1]["frames"] == 80
    assert walk["episodes"][1]["room"] == {"frames_in_motion": 52, "decided_frames": 80, "detected": True,
                                           "latency_s": 5.0}


def test_false_alarms_are_rising_edges_per_hour_of_decided_time(walk):
    s = walk["subjects"]
    b = s[B]["empty"]["false_alarms_per_hour"]
    assert (b["count"], b["frames"]) == (1, 160)
    assert b["hours"] == pytest.approx(40 / 3600, abs=1e-6)
    assert b["rate"] == pytest.approx(90.0)
    low, high = eval_log.wilson(1, 160)
    assert b["ci95"] == pytest.approx([low * 14400, high * 14400], rel=1e-5)  # 160 frames in 40 s: 14400 an hour
    assert s[B]["empty"]["frames_in_motion"]["count"] == 4
    assert s[A]["empty"]["false_alarms_per_hour"]["count"] == 0
    assert s["room"]["empty"]["false_alarms_per_hour"]["rate"] == 0.0   # B alone never made the room MOTION
    assert walk["episodes"][2]["links"][B]["alarms"] == 1


def test_motion_carried_into_an_empty_label_is_no_alarm_but_a_rise_on_its_first_frame_is(tmp_path):
    frames = []
    for i in range(160):
        rel = i * STEP
        a = "MOTION" if 15.0 <= rel < 21.0 else "EMPTY"   # still in MOTION from the walk
        b = "MOTION" if 20.0 <= rel < 22.0 else "EMPTY"   # rises on the empty label's first frame
        frames.append(payload(T0 + rel, [link_row(A, a), link_row(B, b)], "EMPTY"))
    s = score(tmp_path, frames, "10,20,walking\n20,40,empty\n")["subjects"]
    assert s[A]["empty"]["false_alarms_per_hour"]["count"] == 0
    assert s[B]["empty"]["false_alarms_per_hour"]["count"] == 1
    assert s[A]["motion"]["latency_s"]["median"] == pytest.approx(5.0)
    assert s[B]["motion"]["episodes_detected"]["count"] == 0


def test_frames_without_a_decision_miss_in_motion_labels_and_are_not_false_alarm_time(tmp_path):
    frames = []
    for i in range(160):
        rel = i * STEP
        a = "NO_SIGNAL" if 5.0 <= rel < 10.0 else "MOTION" if 25.0 <= rel < 30.0 else "EMPTY"
        room = "WARMUP" if rel < 2.0 else "MOTION" if a == "MOTION" else "EMPTY"
        frames.append(payload(T0 + rel, [link_row(A, a), link_row(C, "UNCALIBRATED")], room))
    result = score(tmp_path, frames, "start_s,end_s,label\n0,20,empty\n20,40,walking\n")
    s = result["subjects"]
    fa = s[A]["empty"]["false_alarms_per_hour"]
    assert fa["frames"] == 60 and fa["hours"] == pytest.approx(15 / 3600, abs=1e-6)        # 5 s of NO_SIGNAL left out
    fa = s["room"]["empty"]["false_alarms_per_hour"]
    assert fa["frames"] == 72 and fa["hours"] == pytest.approx(18 / 3600, abs=1e-6)        # 2 s of WARMUP left out
    assert s[A]["motion"]["frames_in_motion"]["count"] == 20
    assert (s[C]["motion"]["episodes_detected"]["count"], s[C]["motion"]["episodes_detected"]["total"]) == (0, 1)
    assert s[C]["motion"]["frames_in_motion"]["total"] == 80                                # every frame a miss
    assert s[C]["empty"]["false_alarms_per_hour"]["rate"] is None
    assert s[C]["states"] == {"UNCALIBRATED": 160}
    assert any(note.startswith(f"link {C} never had a decision") for note in result["notes"])


def test_localisation_is_the_share_of_frames_whose_hottest_cell_lies_in_the_zone(walk):
    loc = walk["localisation"]
    assert (loc["frames"], loc["hot_frames"]) == (80, 56)          # the 24 cold frames count as misses
    assert loc["in_zone"]["count"] == 40 and loc["in_zone"]["rate"] == pytest.approx(0.5)
    assert loc["in_zone_when_hot"]["rate"] == pytest.approx(40 / 56)
    assert loc["median_distance_m"] == 0.0
    assert walk["episodes"][1]["localisation"]["inside"] == 40


def test_a_map_at_or_below_min_heat_is_cold_and_a_miss_has_a_distance(tmp_path):
    frames = [payload(T0 + i * STEP, [link_row(A, "MOTION")], "MOTION", hot=3, heat=0.04 if i < 20 else 0.5)
              for i in range(40)]
    loc = score(tmp_path, frames, '0,10,walking,"0,0,2,1"\n')["localisation"]
    assert (loc["frames"], loc["hot_frames"], loc["in_zone"]["count"]) == (40, 20, 0)
    assert loc["median_distance_m"] == pytest.approx(1.5)       # the cell centre (3.5, 0.5) is 1.5 m right of it
    result = score(tmp_path, frames, '0,10,walking,"0,0,2,1"\n', min_heat=0.6)
    assert result["localisation"]["hot_frames"] == 0
    assert any("nothing to localise" in note for note in result["notes"])


def test_the_scaling_curve_votes_k_of_n_over_every_link_subset(tmp_path):
    """A and B are in MOTION for the whole walk and C never is; C false-alarms twice while the room is empty."""
    frames = []
    for i in range(160):
        rel = i * STEP
        walking = 20.0 <= rel < 40.0
        a = "MOTION" if walking else "EMPTY"
        c = "MOTION" if 5.0 <= rel < 6.0 or 10.0 <= rel < 10.5 else "EMPTY"
        frames.append(payload(T0 + rel, [link_row(A, a), link_row(B, a), link_row(C, c)],
                              "MOTION" if walking else "EMPTY"))
    result = score(tmp_path, frames, "0,20,empty\n20,40,walking\n", ks=[1, 2])
    by_k = {curve["k"]: curve["points"] for curve in result["scaling"]}
    two = by_k[2]
    assert [(p["links"], p["k"], p["subsets"], p["all_subsets"]) for p in two] == [
        (1, 1, 3, True), (2, 2, 3, True), (3, 2, 1, True)]                          # k is capped at the subset size
    assert [p["episodes_detected"]["rate"] for p in two] == pytest.approx([2 / 3, 1 / 3, 1.0], abs=1e-6)
    assert [p["false_alarms_per_hour"]["count"] for p in two] == pytest.approx([2 / 3, 0, 0], abs=1e-6)
    assert two[0]["frames_in_motion"]["range"] == [0.0, 1.0]
    pair = two[1]["per_subset"][0]
    assert pair["links"] == [A, B] and pair["episodes_detected"] == 1 and pair["frames_in_motion"] == 80
    assert pair["alarms"] == 0 and pair["observed_h"] == pytest.approx(20 / 3600, abs=1e-6)
    one = by_k[1]
    assert [p["false_alarms_per_hour"]["count"] for p in one] == pytest.approx([2 / 3, 4 / 3, 2], abs=1e-6)
    assert one[2]["false_alarms_per_hour"]["rate"] == pytest.approx(2 / (20 / 3600))


def test_the_scaling_curve_with_every_link_follows_the_rooms_two_link_rule(walk):
    curve = walk["scaling"][0]
    assert curve["k"] == 2
    single, both = curve["points"]
    assert single["frames_in_motion"]["rate"] == pytest.approx(62 / 80)
    assert single["frames_in_motion"]["range"] == [0.65, 0.9]
    assert single["false_alarms_per_hour"]["rate"] == pytest.approx(45.0)    # B's 90 an hour averaged with A's 0
    assert both["frames_in_motion"]["count"] == walk["subjects"]["room"]["motion"]["frames_in_motion"]["count"]
    assert both["false_alarms_per_hour"]["count"] == 0 and both["median_latency_s"] == pytest.approx(5.0)


def test_other_labels_report_their_share_of_frames_in_motion(tmp_path):
    frames = [payload(T0 + i * STEP, [link_row(A, "MOTION" if 2 <= i * STEP < 4 or 20 <= i * STEP < 30 else "EMPTY")],
                      "EMPTY") for i in range(160)]
    result = score(tmp_path, frames, '0, 10, still_standing\n10, 20, Still_Standing\n20, 40, WALKING, "0, 0, 1, 1"\n')
    other = result["subjects"][A]["other"]
    assert list(other) == ["still_standing"]
    assert (other["still_standing"]["count"], other["still_standing"]["total"]) == (8, 80)
    assert result["labels"] == {"rows": 3, "motion": 1, "empty": 0, "other": 2}
    assert result["subjects"][A]["motion"]["episodes_detected"]["count"] == 1      # labels match in any case
    assert any(note.startswith("no empty labels") for note in result["notes"])


def test_link_health_is_summarised_and_a_gain_change_is_flagged(walk):
    health = walk["health"]
    assert health[A]["seq_rate"] == 38.46 and health[A]["rssi"] == -50 and health[B]["rssi"] == -60
    assert health[A]["seq_loss"] is None                                  # this log has no seq_loss
    assert health[A]["agc_gain"] == {"values": [30.0], "changes": 0}
    assert health[B]["agc_gain"] == {"values": [30.0, 34.0], "changes": 1}
    assert any(note.startswith(f"link {B}: agc_gain changed 1 time") for note in walk["notes"])


def test_the_summary_has_every_section_and_the_result_is_strict_json(walk):
    text = eval_log.format_summary(walk)
    for heading in ("detection during motion labels", "false alarms during empty labels", "episodes (room state",
                    "heatmap localisation", "scaling, k=2", "link health", "notes:"):
        assert heading in text
    json.dumps(walk, allow_nan=False)


# ---------------------------------------------------------------------------------------------- labels

def test_labels_take_a_header_comments_extra_columns_zones_and_an_open_end(tmp_path):
    path = write_labels(tmp_path / "labels.csv",
                        "﻿# walk protocol\n Start_S , End_S , Label , Zone, note\n0, 10, empty, , quiet\n"
                        '10,20,Walking,"1.5;0.5 0.5,1.0",x\n20,,empty\n')
    labels = eval_log.read_labels(path)
    assert labels[0] == eval_log.Label(0.0, 10.0, "empty", None, 3)
    assert labels[1].label == "Walking" and labels[1].zone == (0.5, 0.5, 1.5, 1.0)
    assert labels[2].end is None


def test_labels_without_a_header_take_the_columns_in_order_and_an_unquoted_zone(tmp_path):
    path = write_labels(tmp_path / "labels.csv", '0,10,empty,,\n10, 20, walking, "0, 0, 1, 1"\n20,30,walking,2,1,0,0\n')
    assert [(lab.start, lab.end, lab.label, lab.zone) for lab in eval_log.read_labels(path)] == [
        (0.0, 10.0, "empty", None), (10.0, 20.0, "walking", (0.0, 0.0, 1.0, 1.0)),
        (20.0, 30.0, "walking", (0.0, 0.0, 2.0, 1.0))]


@pytest.mark.parametrize("text, message", [
    ("start_s,label\n0,empty\n", "end_s missing"),
    ("0,0,empty\n", "line 1: end_s 0 is not after start_s 0"),
    ('0,5,walking,"0,0,1"\n', "is not four numbers"),
    ('0,5,walking,"0,0,0,1"\n', "has no area"),
    ("x,5,empty\n", "is not a number of seconds"),
    ("0,5,\n", "needs start_s, end_s and label"),
    ("# only a comment\n", "has no labels"),
])
def test_bad_label_files_are_refused_with_the_reason(tmp_path, text, message):
    path = write_labels(tmp_path / "labels.csv", text)
    with pytest.raises(eval_log.EvalError) as excinfo:
        eval_log.read_labels(path)
    assert message in str(excinfo.value)


def test_absolute_label_times_take_unix_seconds_and_iso_dates(tmp_path):
    path = write_labels(tmp_path / "labels.csv", "2023-11-14T22:13:20Z,1700000010,empty\n")
    labels = eval_log.read_labels(path, absolute=True)
    assert (labels[0].start, labels[0].end) == (1_700_000_000.0, 1_700_000_010.0)
    with pytest.raises(eval_log.EvalError, match="--absolute"):
        eval_log.read_labels(path)


def test_absolute_labels_score_like_relative_ones_and_an_open_end_runs_to_the_end(tmp_path):
    labels = f"{T0},{T0 + 20},empty\n{T0 + 20},{T0 + 40},walking\n{T0 + 40},,empty\n"
    result = score(tmp_path, walk_frames(), labels, absolute=True)
    assert result["subjects"][B]["motion"]["frames_in_motion"]["count"] == 52
    assert result["subjects"][B]["empty"]["false_alarms_per_hour"]["count"] == 1
    assert result["episodes"][2]["end_s"] == T0 + 60.0


def test_labels_that_miss_the_log_suggest_the_other_time_base(tmp_path):
    log = eval_log.read_log(write_log(tmp_path / "run.jsonl", walk_frames()))
    host_times = eval_log.read_labels(write_labels(tmp_path / "a.csv", f"{T0},{T0 + 20},empty\n"))
    with pytest.raises(eval_log.EvalError, match="pass --absolute"):
        eval_log.evaluate(log, host_times)
    relative = eval_log.read_labels(write_labels(tmp_path / "r.csv", "0,20,empty\n"), absolute=True)
    with pytest.raises(eval_log.EvalError, match="leave out --absolute"):
        eval_log.evaluate(log, relative, absolute=True)


def test_labels_partly_or_wholly_outside_the_log_are_noted(tmp_path):
    result = score(tmp_path, walk_frames(), "0,20,empty\n55,70,walking\n100,110,walking\n")
    notes = "\n".join(result["notes"])
    assert "label 2 (line 2: walking 55-70 s) reaches outside the log" in notes
    assert "label 3 (line 3: walking 100-110 s) has no frames in the log" in notes
    assert result["episodes"][2]["room"] is None
    assert result["subjects"]["room"]["motion"]["episodes_detected"]["total"] == 1


# ---------------------------------------------------------------------------------------------- the log

def test_a_cut_off_last_line_is_skipped_with_a_note_but_a_corrupt_line_inside_is_an_error(tmp_path):
    frames = walk_frames()[:8]
    log = eval_log.read_log(write_log(tmp_path / "cut.jsonl", frames, tail='{"links": [{"id": "rx1<'))
    assert len(log.times) == 8 and "incomplete" in log.notes[0]
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text(json.dumps(header()) + "\n{not json\n" + json.dumps(frames[0]) + "\n", encoding="utf-8")
    with pytest.raises(eval_log.EvalError, match="line 2 is corrupt"):
        eval_log.read_log(corrupt)


@pytest.mark.parametrize("text, message", [
    ('{"format": "other"}\n', "not a dashboard log header"),
    (json.dumps(header()) + "\n", "no state frames"),
    ("", "is empty"),
    (json.dumps(header()) + '\n{"links": []}\n', "not a state payload"),
])
def test_files_that_are_not_dashboard_logs_are_refused(tmp_path, text, message):
    path = tmp_path / "run.jsonl"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(eval_log.EvalError, match=message):
        eval_log.read_log(path)
    with pytest.raises(eval_log.EvalError, match="cannot read log"):
        eval_log.read_log(tmp_path / "missing.jsonl")


def test_the_pipelines_minimal_last_frame_and_a_late_frame_still_load(tmp_path):
    frames = walk_frames()[:8]
    failed = {"links": [], "heatmap": {}, "status": {"time_s": T0 + 2.0, "finished": True, "errors": {},
                                                    "messages": ["run ended: the pipeline failed"]}}
    log = eval_log.read_log(write_log(tmp_path / "failed.jsonl", frames + [failed]))
    assert len(log.times) == 9 and log.hot[-1] is None
    assert log.states[A]["absent"] == 1 and log.states["room"]["none"] == 1
    late = eval_log.read_log(write_log(tmp_path / "late.jsonl", frames + [frames[3]]))
    assert late.times == sorted(late.times) and "out of time order" in late.notes[0]


def test_logs_from_before_the_room_decision_and_the_health_keys_still_score(tmp_path):
    frames = [{"links": [{"id": A, "state": "MOTION" if i >= 20 else "EMPTY", "pkt_rate": 20.0, "rssi": -40,
                          "drops": 3}],
               "heatmap": {"cols": 2, "rows": 1, "cells": [0.0, 0.5]}, "room": {"width_m": 2.0, "depth_m": 1.0},
               "status": {"time_s": T0 + i * STEP}} for i in range(40)]
    result = score(tmp_path, frames, '0,5,empty\n5,10,walking,"1,0,2,1"\n')
    assert result["subjects"][A]["motion"]["episodes_detected"]["count"] == 1
    assert result["subjects"]["room"]["motion"]["episodes_detected"]["count"] == 0
    assert any("room state never had a decision" in note for note in result["notes"])
    assert result["health"][A]["pkt_rate"] == 20.0 and result["health"][A]["seq_rate"] is None
    assert result["health"][A]["drops"] == 3
    assert result["localisation"]["in_zone"]["rate"] == 1.0     # no extents: the room's size places the cells


def test_the_log_format_is_the_one_the_dashboard_writes():
    from csi.server import LOG_FORMAT

    assert eval_log.LOG_FORMAT == LOG_FORMAT


# ---------------------------------------------------------------------------------------------- command line

def test_the_command_line_prints_a_summary_and_writes_the_json_result(tmp_path, capsys):
    log = write_log(tmp_path / "run.jsonl", walk_frames())
    labels = write_labels(tmp_path / "labels.csv", WALK_LABELS)
    out = tmp_path / "result.json"
    assert eval_log.main(["--log", str(log), "--labels", str(labels), "--out", str(out), "--k", "1,2"]) == 0
    printed = capsys.readouterr().out
    assert "detection during motion labels" in printed and "90.0/h [15.9-498] 1 in 0.0111 h" in printed
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["labels"]["path"] == str(labels) and [curve["k"] for curve in result["scaling"]] == [1, 2]


def test_the_command_line_reports_an_unusable_input_and_returns_1(tmp_path, capsys):
    labels = write_labels(tmp_path / "labels.csv", WALK_LABELS)
    assert eval_log.main(["--log", str(tmp_path / "missing.jsonl"), "--labels", str(labels)]) == 1
    assert capsys.readouterr().err.startswith("error: cannot read log")


@pytest.mark.parametrize("option", [["--k", "0"], ["--k", "two"], ["--max-subsets", "0"], ["--min-heat", "1.5"]])
def test_the_command_line_refuses_bad_options(option):
    with pytest.raises(SystemExit):
        eval_log.build_parser().parse_args(["--log", "run.jsonl", "--labels", "labels.csv", *option])


@pytest.mark.slow
def test_scores_a_log_written_by_the_dashboard(tmp_path):
    """A real --log from a simulated walk: every link and frame is read, and the counts match the payloads."""
    from csi.room import Walk
    from csi.server import DashboardServer
    from tests.test_dashboard_server import LABELS, ROOM, calibrated_thresholds, quiet_then_streams

    log_path = tmp_path / "walk.jsonl"
    person = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5)
    server = DashboardServer(ROOM, quiet_then_streams(person, duration_s=3.0, seed=4), calibrated_thresholds(),
                             port=0, log=log_path, log_meta={"source": "simulate-room"})
    server.start()
    try:
        assert server.wait_finished(timeout=60)
    finally:
        server.stop()
    payloads = [json.loads(line) for line in log_path.read_text().splitlines()[1:]]
    labels = write_labels(tmp_path / "labels.csv", "start_s,end_s,label\n0,,walking\n")   # the whole run

    result = eval_log.evaluate(eval_log.read_log(log_path), eval_log.read_labels(labels))

    assert result["links"] == sorted(LABELS)
    assert result["log"]["frames"] == len(payloads)
    room_motion = sum(p["status"]["room"]["state"] == "MOTION" for p in payloads)
    assert result["subjects"]["room"]["motion"]["frames_in_motion"]["count"] == room_motion
    for label in LABELS:
        in_motion = sum(row["id"] == label and row["state"] == "MOTION" for p in payloads for row in p["links"])
        assert result["subjects"][label]["motion"]["frames_in_motion"]["count"] == in_motion
    assert [point["subsets"] for point in result["scaling"][0]["points"]] == [4, 6, 4, 1]
