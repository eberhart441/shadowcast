import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

from csi.detect import detect_packets
from csi.evaluate import EvaluationError, evaluate_sessions, format_report
from csi.recording import load_session, record_session
from csi.simulate import Segment, simulate_stream

ROOT = Path(__file__).resolve().parents[1]
SIM_STATE = {"empty": "empty", "walking": "motion",
             "still_standing": "still", "still_sitting_or_lying": "still"}
_RECORDED: dict[tuple, Path] = {}
_CACHE_DIR = Path(tempfile.mkdtemp(prefix="csi-recorded-plans-"))
atexit.register(shutil.rmtree, _CACHE_DIR, True)


def record_plan(folder: Path, plan: list[tuple[str, float]], seed: int = 0) -> Path:
    """Record a simulator session into ``folder``. Each (plan, seed) is recorded once per process and copied."""
    key = (tuple(plan), seed)
    if key not in _RECORDED:
        built = _CACHE_DIR / f"plan{len(_RECORDED)}"
        _record_plan(built, plan, seed)
        _RECORDED[key] = built
    shutil.copytree(_RECORDED[key], folder)
    return folder


def _record_plan(folder: Path, plan: list[tuple[str, float]], seed: int) -> Path:
    scenario = [Segment(SIM_STATE[label], secs) for label, secs in plan]
    starts = np.cumsum([0] + [round(secs * 100) for _, secs in plan])
    switches = {int(starts[i]): label for i, (label, _) in enumerate(plan) if i > 0}
    calls = {"n": -1}

    def poll():
        calls["n"] += 1
        return switches.get(calls["n"])

    record_session(simulate_stream(scenario=scenario, rate_hz=100.0, seed=seed), folder,
                   plan[0][0], send_rate_hz=100.0, countdown_s=0.0, poll_label=poll)
    return folder


def test_walking_and_empty_land_on_their_diagonal(tmp_path):
    # a switch to the same label is a no-op for the recorder, so separate the two empties
    # margin 1.5 (not the default 1.0): this asserts zero empty false alarms, which 1.0 does not give on the simulator
    folder = record_plan(tmp_path / "room",
                         [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 20)])
    (result,) = evaluate_sessions([folder], margin=1.5).sessions
    cm = result.confusion
    assert cm.loc["empty", "EMPTY"] > 0.9 * cm.loc["empty"].sum()
    assert cm.loc["empty", "MOTION"] == 0
    assert cm.loc["walking", "MOTION"] > 0.9 * cm.loc["walking"].sum()
    assert cm.loc["walking", "EMPTY"] == 0


def test_still_labels_are_reported_on_separate_rows(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("still_standing", 20), ("still_sitting_or_lying", 20), ("empty", 20)])
    cm = evaluate_sessions([folder]).sessions[0].confusion
    assert cm.loc["still_standing"].sum() > 0
    assert cm.loc["still_sitting_or_lying"].sum() > 0


@pytest.mark.slow
def test_still_rows_are_still_present_not_motion_on_a_simulated_session(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("still_standing", 40), ("empty", 20), ("still_sitting_or_lying", 40)])
    # margin 1.5: at the default 1.0 the simulated still person reads MOTION (see test_calibration_margin.py)
    result = evaluate_sessions([folder], breathing=True, margin=1.5).sessions[0]
    recordings = load_session(folder).recordings
    for label, rec in [("still_standing", recordings[1]), ("still_sitting_or_lying", recordings[3])]:
        assert rec.label == label
        readings = list(detect_packets(rec.packets(), result.calibration.threshold, breathing=True))
        with_breathing = sum(r.enough_data for r in readings)
        cm = result.confusion
        assert cm.loc[label, "MOTION"] == 0
        assert cm.loc[label, "STILL_PRESENT"] >= 0.9 * with_breathing > 0


def test_default_evaluation_is_motion_only_and_never_predicts_still_present(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("walking", 10), ("empty", 20), ("still_standing", 30)])
    report = evaluate_sessions([folder])
    assert report.sessions[0].confusion["STILL_PRESENT"].sum() == 0
    assert report.total()["STILL_PRESENT"].sum() == 0
    text = format_report(report)
    assert "breathing disabled" in text
    assert "decisions without a full" not in text


def test_breathing_opt_in_scores_still_rows_and_reports_breathing_window(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("walking", 10), ("empty", 20), ("still_standing", 30)])
    report = evaluate_sessions([folder], breathing=True, margin=1.5)  # 1.0 reads the still person as MOTION
    assert report.sessions[0].confusion.loc["still_standing", "STILL_PRESENT"] > 0
    text = format_report(report)
    assert "breathing disabled" not in text
    assert "decisions without a full" in text


def test_calibration_recording_is_excluded_from_scoring(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    result = evaluate_sessions([folder]).sessions[0]
    second_empty = load_session(folder).recordings[2]
    assert result.calibration_file == load_session(folder).recordings[0].path.name
    expected = [r.state for r in detect_packets(second_empty.packets(), result.calibration.threshold)]
    assert int(result.confusion.loc["empty"].sum()) == len(expected)


def test_single_empty_recording_scores_no_empty_windows_and_says_so(tmp_path):
    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10)])
    result = evaluate_sessions([folder]).sessions[0]
    assert result.confusion.loc["empty"].sum() == 0
    assert result.confusion.loc["walking"].sum() > 0
    assert any("only one empty recording" in n for n in result.notes)


def test_missing_empty_recording_fails_with_actionable_message(tmp_path):
    folder = record_plan(tmp_path / "room", [("walking", 20)])
    with pytest.raises(EvaluationError, match="no empty recording.*--label empty"):
        evaluate_sessions([folder])


def test_unusable_calibration_recording_fails_clearly(tmp_path):
    folder = record_plan(tmp_path / "room", [("empty", 5), ("walking", 10)])
    with pytest.raises(EvaluationError, match="cannot calibrate.*too short"):
        evaluate_sessions([folder])


@pytest.mark.slow
def test_multiple_sessions_in_one_run_sum_into_total(tmp_path):
    a = record_plan(tmp_path / "a", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    b = record_plan(tmp_path / "b", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)], seed=3)
    report = evaluate_sessions([a, b])
    assert [s.name for s in report.sessions] == ["a", "b"]
    assert (report.total() == report.sessions[0].confusion + report.sessions[1].confusion).all().all()
    assert "all sessions" in format_report(report)


def test_evaluation_is_deterministic(tmp_path):
    folder = record_plan(tmp_path / "room", [
        ("empty", 20), ("walking", 10), ("empty", 20), ("still_standing", 20)])
    first = evaluate_sessions([folder]).total()
    second = evaluate_sessions([folder]).total()
    assert first.equals(second)


@pytest.mark.slow
def test_cli_is_motion_only_by_default_and_takes_breathing_flag(tmp_path):
    good = record_plan(tmp_path / "good", [
        ("empty", 20), ("walking", 10), ("empty", 20), ("still_standing", 30)])
    script = str(ROOT / "scripts" / "evaluate.py")
    default = subprocess.run([sys.executable, script, str(good)], cwd=ROOT, capture_output=True, text=True)
    assert default.returncode == 0, default.stderr
    assert "breathing disabled" in default.stdout
    opted = subprocess.run([sys.executable, script, "--breathing", str(good)],
                           cwd=ROOT, capture_output=True, text=True)
    assert opted.returncode == 0, opted.stderr
    assert "decisions without a full" in opted.stdout


@pytest.mark.slow
def test_cli_prints_matrix_and_fails_clearly_without_empty(tmp_path):
    good = record_plan(tmp_path / "good", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    bad = record_plan(tmp_path / "bad", [("walking", 10)])
    script = str(ROOT / "scripts" / "evaluate.py")
    ok = subprocess.run([sys.executable, script, str(good)], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert "EMPTY" in ok.stdout and "MOTION" in ok.stdout and "walking" in ok.stdout
    fail = subprocess.run([sys.executable, script, str(bad)], cwd=ROOT, capture_output=True, text=True)
    assert fail.returncode == 1
    assert "no empty recording" in fail.stderr


# --- calibration margin: applied to the per-session calibration, printed, and monotone in motion ---

def test_evaluate_margin_scales_the_session_threshold_and_is_printed(tmp_path):
    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    base = evaluate_sessions([folder], margin=1.0).sessions[0]
    doubled_report = evaluate_sessions([folder], margin=2.0)
    assert doubled_report.sessions[0].calibration.margin == 2.0
    assert doubled_report.sessions[0].calibration.threshold == pytest.approx(2 * base.calibration.threshold, rel=1e-9)
    assert "margin 2.00" in format_report(doubled_report)


@pytest.mark.slow
def test_larger_margin_never_detects_more_motion_in_evaluation(tmp_path):
    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    motion = []
    for m in (0.5, 1.0, 2.0, 4.0):
        cm = evaluate_sessions([folder], margin=m).sessions[0].confusion
        motion.append(int(cm["MOTION"].sum()))
    assert motion == sorted(motion, reverse=True)


@pytest.mark.slow
def test_cli_margin_is_accepted_printed_and_validated(tmp_path):
    good = record_plan(tmp_path / "good", [("empty", 20), ("walking", 10), ("empty", 20), ("walking", 10)])
    script = str(ROOT / "scripts" / "evaluate.py")
    ok = subprocess.run([sys.executable, script, "--margin", "1.5", str(good)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert "margin 1.50" in ok.stdout
    bad = subprocess.run([sys.executable, script, "--margin", "0", str(good)],
                         cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode != 0
    assert "margin" in bad.stderr
