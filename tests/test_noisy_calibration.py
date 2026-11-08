"""Noisy-room thresholds: the formula in docs/agents/hardware.md, as a pure function of per-link scores.

Expected values are worked by hand from the formula (median, lower-half spread, max(...)), not computed by the
code under test. Scores 1..40 are used throughout: median 20.5, lower half 1..20 whose deviations 19.5..0.5 have
median 10.0, so the formula gives 20.5 + 6 x 1.4826 x 10 = 109.456 (and the 3 x median floor, 61.5, is lower).
"""
import pytest

from csi.noisy_calibration import noisy_room_thresholds

CLEAN_40 = [float(v) for v in range(1, 41)]
CLEAN_THRESHOLD = 109.456


def one(scores):
    return noisy_room_thresholds({"rx1<-00": scores})["rx1<-00"]


def test_threshold_is_the_formula_on_a_clean_link():
    result = one(CLEAN_40)

    assert result.threshold == pytest.approx(CLEAN_THRESHOLD, abs=1e-9)
    assert result.reason is None
    assert result.median == pytest.approx(20.5)
    assert result.spread == pytest.approx(10.0)
    assert result.max == 40.0
    assert result.readings == 40
    assert result.empty == 0


def test_the_floor_of_three_times_the_median_applies_when_the_spread_is_zero():
    result = one([10.0] * 40)

    assert result.spread == 0.0
    assert result.threshold == pytest.approx(30.0)  # max(10 + 0, 3 x 10)


def test_none_scores_are_skipped_and_counted_as_empty_readings():
    interleaved = []
    for score in CLEAN_40:
        interleaved += [score, None] if score <= 10 else [score]  # ten Nones, after each of 1..10
    result = one(interleaved)

    assert result.readings == 50
    assert result.empty == 10
    assert result.threshold == pytest.approx(CLEAN_THRESHOLD, abs=1e-9)
    assert result.reason is None


def test_exactly_half_usable_and_forty_usable_is_still_calibrated():
    result = one(CLEAN_40 + [None] * 40)  # 40 of 80 usable: 50% and 40 scores, both at the limit

    assert result.reason is None
    assert result.threshold == pytest.approx(CLEAN_THRESHOLD, abs=1e-9)


def test_a_link_with_fewer_than_half_usable_readings_gets_no_threshold_and_a_reason():
    result = one(CLEAN_40[:30] + [None] * 40)  # 30 of 70 usable: under half

    assert result.threshold is None
    assert "usable" in result.reason
    assert result.empty == 40
    assert result.median == pytest.approx(15.5)  # stats are still reported for the operator


def test_a_link_with_fewer_than_forty_scores_gets_no_threshold_and_a_reason():
    result = one(CLEAN_40[:39])  # all usable, but one short of 40

    assert result.threshold is None
    assert "40" in result.reason


def test_an_all_none_link_gets_no_threshold_and_no_statistics():
    result = one([None] * 50)

    assert result.threshold is None
    assert result.reason
    assert result.readings == 50
    assert result.empty == 50
    assert result.median is None
    assert result.spread is None
    assert result.max is None


def test_a_movement_spike_above_the_median_does_not_inflate_the_spread():
    # Three movement scores at 500 among 40 quiet ones: the median moves to 22 (43 values), the lower half
    # is 1..22, whose deviations have median 10.5. Threshold = 22 + 6 x 1.4826 x 10.5 = 115.4038.
    spiked = CLEAN_40 + [500.0] * 3
    result = one(spiked)

    assert result.max == 500.0
    assert result.median == pytest.approx(22.0)
    assert result.spread == pytest.approx(10.5)
    assert result.threshold == pytest.approx(115.4038, abs=1e-4)
    assert result.threshold < 500.0  # the spike never becomes the threshold


def test_each_link_is_judged_on_its_own():
    results = noisy_room_thresholds({"rx1<-00": CLEAN_40, "rx2<-00": [None] * 50, "rx1<-01": [10.0] * 40})

    assert list(results) == ["rx1<-00", "rx2<-00", "rx1<-01"]
    assert results["rx1<-00"].threshold == pytest.approx(CLEAN_THRESHOLD, abs=1e-9)
    assert results["rx2<-00"].threshold is None
    assert results["rx1<-01"].threshold == pytest.approx(30.0)
