"""The score code: a link report's u8 motion score, on a log scale (10 codes per octave, code 0 = no score)."""
import math

import pytest

from csi.score_code import NO_SCORE, decode_score, encode_score


def test_no_score_is_code_zero_both_ways():
    assert encode_score(None) == NO_SCORE == 0
    assert decode_score(0) is None


@pytest.mark.parametrize("code, score", [(1, 2 ** -10), (101, 1.0), (111, 2.0), (91, 0.5), (255, 2 ** 15.4)])
def test_codes_decode_to_the_documented_grid(code, score):
    assert decode_score(code) == pytest.approx(score, rel=1e-12)
    assert encode_score(score) == code


def test_each_code_step_is_a_tenth_of_an_octave():
    assert decode_score(102) / decode_score(101) == pytest.approx(2 ** 0.1, rel=1e-12)


@pytest.mark.parametrize("score", [0.001, 0.0137, 0.2, 1.0, 3.3, 47.0, 999.0, 30_000.0])
def test_round_trip_stays_within_half_a_step(score):
    back = decode_score(encode_score(score))
    assert abs(math.log2(back / score)) <= 0.05 + 1e-12


@pytest.mark.parametrize("score", [0.0, -1.0, 1e-9, 2 ** -10.06])
def test_scores_at_or_below_the_floor_saturate_at_code_one(score):
    assert encode_score(score) == 1


@pytest.mark.parametrize("score", [43_238.0 * 1.04, 1e9, math.inf])
def test_scores_above_the_top_saturate_at_code_255(score):
    assert encode_score(score) == 255


def test_codes_increase_with_the_score():
    scores = [2 ** (e / 37) for e in range(-400, 600)]
    codes = [encode_score(s) for s in scores]
    assert codes == sorted(codes)


@pytest.mark.parametrize("code", [-1, 256])
def test_decode_rejects_codes_outside_a_byte(code):
    with pytest.raises(ValueError):
        decode_score(code)


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        encode_score(math.nan)
