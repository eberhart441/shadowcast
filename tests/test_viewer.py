import re

import numpy as np
import pytest

from csi.simulate import Segment, simulate_stream
from csi.viewer import DEFAULT_THRESHOLD, LiveView

N_KEPT = 52  # 64 subcarriers minus 12 nulls


def feed(view: LiveView, stream) -> LiveView:
    view.consume(stream)
    return view


def test_heatmap_has_one_row_per_kept_subcarrier_and_one_column_per_packet():
    view = feed(LiveView(), simulate_stream(scenario=[Segment("empty", 3)], rate_hz=100.0))
    heat = view.heatmap()
    assert heat.shape[0] == N_KEPT
    assert heat.shape[1] == pytest.approx(300, abs=2)


def test_heatmap_times_match_heatmap_columns_and_end_at_latest_packet():
    view = feed(LiveView(), simulate_stream(scenario=[Segment("empty", 3)], rate_hz=100.0))
    times = view.heatmap_times()
    assert times.size == view.heatmap().shape[1]
    assert times[-1] == pytest.approx(0.0, abs=0.02)
    assert times[0] == pytest.approx(-2.99, abs=0.05)


def test_heatmap_history_is_bounded_to_ten_seconds():
    view = feed(LiveView(), simulate_stream(scenario=[Segment("empty", 25)], rate_hz=100.0))
    assert view.heatmap().shape[1] == pytest.approx(1000, abs=2)


def test_variance_trace_is_higher_during_motion_than_empty():
    empty_view = feed(LiveView(), simulate_stream(scenario=[Segment("empty", 12)], seed=7))
    motion_view = feed(LiveView(), simulate_stream(
        scenario=[Segment("empty", 10), Segment("motion", 10)], seed=7))
    _, empty = empty_view.variance_trace()
    _, motion = motion_view.variance_trace()
    assert empty.size and motion.size
    assert motion.mean() > 10 * empty.mean()


def test_variance_trace_times_are_relative_to_latest_packet():
    view = feed(LiveView(), simulate_stream(scenario=[Segment("empty", 5)], rate_hz=100.0))
    times, scores = view.variance_trace()
    assert scores.size == times.size and scores.size > 0
    assert times[-1] == pytest.approx(0.0, abs=0.02)
    assert np.all(np.diff(times) > 0)


def test_threshold_starts_at_the_value_given():
    assert LiveView(threshold=3.5).threshold == 3.5


def test_default_threshold_used_when_none_given():
    assert LiveView().threshold == DEFAULT_THRESHOLD


def test_adjust_threshold_raises_and_lowers_and_is_reflected_in_title():
    view = LiveView(threshold=2.0)
    view.adjust_threshold(+2)
    assert view.threshold > 2.0
    assert f"threshold {view.threshold:.2f}" in view.title()
    assert view.threshold == pytest.approx(2.0 * 1.25**2)
    view.adjust_threshold(-2)
    assert view.threshold == pytest.approx(2.0)


def test_title_reports_packet_rate_and_drop_count():
    stream = simulate_stream(scenario=[Segment("empty", 20)], rate_hz=100.0, seed=3, drop_prob=0.1)
    view = feed(LiveView(send_rate_hz=100.0), stream)
    true_drops = 2000 - view.parser.stats.parsed
    assert true_drops > 100

    rate = float(re.search(r"([\d.]+) pkt/s", view.title()).group(1))
    drops = int(re.search(r"drops (\d+)", view.title()).group(1))
    assert rate == pytest.approx(90.0, rel=0.15)
    assert abs(drops - true_drops) <= 0.1 * true_drops


def test_malformed_lines_are_counted_and_do_not_stop_the_view():
    clean = list(simulate_stream(scenario=[Segment("empty", 5)], rate_hz=100.0))
    bad = ["CSI_DATA,garbage", "CSI_DATA,1,2,[1,2", "CSI_DATA,1,2,3,[1,2,"]
    stream, injected = [], 0
    for i, item in enumerate(clean):
        stream.append(item)
        if i % 100 == 50:
            stream.append((item[0], bad[injected % len(bad)]))
            injected += 1
    view = feed(LiveView(), stream)
    assert injected == 5
    assert view.parser.stats.malformed == injected
    assert view.heatmap().shape[0] == N_KEPT
    assert view.variance_trace()[1].size > 0
