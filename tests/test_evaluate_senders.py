"""Evaluation of a recording that holds several senders heard by one receiver.

Each sender is its own link with its own signal scale: calibrated and detected on its own, and a window is MOTION
when any link says MOTION at that time. The recordings are simulator sessions written through the real recorder
and read back through the CSV loader, so the mac column is what the evaluator sees.
"""
import heapq
from operator import itemgetter


from csi.evaluate import evaluate_sessions, format_report
from csi.recording import record_session
from csi.simulate import Segment, simulate_stream

MAC_A, MAC_B = "1a:00:00:00:00:00", "1a:00:00:00:00:01"


def two_senders(scenario, seed):
    """Both senders' lines on one receiver, merged in host-time order (each has its own baseline and noise)."""
    a = simulate_stream(scenario=scenario, seed=seed, mac=MAC_A)
    b = simulate_stream(scenario=scenario, seed=seed + 100, mac=MAC_B)
    return heapq.merge(a, b, key=itemgetter(0))


def one_sender(scenario, seed):
    return simulate_stream(scenario=scenario, seed=seed, mac=MAC_A)


def session(folder, plan, make_stream):
    for label, state, seconds, seed in plan:
        record_session(make_stream([Segment(state, seconds)], seed), folder, label,
                       send_rate_hz=100.0, countdown_s=0.0)
    return folder


def test_two_senders_on_one_recording_are_calibrated_and_scored_per_link(tmp_path):
    folder = session(tmp_path / "room", [("empty", "empty", 20, 1), ("empty", "empty", 20, 2),
                                         ("walking", "motion", 10, 3)], two_senders)

    (result,) = evaluate_sessions([folder], margin=1.5).sessions

    assert set(result.links) == {MAC_A, MAC_B}            # one calibration per sender link
    assert result.calibration is None                     # no single session-wide threshold
    cm = result.confusion
    assert cm.loc["empty", "EMPTY"] > 0.9 * cm.loc["empty"].sum()
    assert cm.loc["empty", "MOTION"] < 0.1 * cm.loc["empty"].sum()
    assert cm.loc["walking", "MOTION"] > 0.9 * cm.loc["walking"].sum()


def test_the_report_names_each_sender_link_and_says_how_the_links_are_combined(tmp_path):
    folder = session(tmp_path / "room", [("empty", "empty", 20, 1), ("empty", "empty", 20, 2),
                                         ("walking", "motion", 10, 3)], two_senders)
    text = format_report(evaluate_sessions([folder], margin=1.5))

    assert MAC_A in text and MAC_B in text
    assert "any link" in text


def test_a_single_sender_session_keeps_one_session_threshold_as_before(tmp_path):
    folder = session(tmp_path / "room", [("empty", "empty", 20, 1), ("empty", "empty", 20, 2),
                                         ("walking", "motion", 10, 3)], one_sender)

    (result,) = evaluate_sessions([folder], margin=1.5).sessions

    assert result.links == {}
    assert result.calibration is not None and result.calibration.threshold > 0
