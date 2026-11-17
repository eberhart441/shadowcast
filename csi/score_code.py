"""Score code: the motion score as one byte, the way a link report carries it.

The scale is logarithmic, 10 codes per octave, so every step is a factor of 2 ** 0.1 (about 7.2 %) at any score
and a score inside the range comes back within a factor 2 ** +-0.05 (about 3.5 %). Code 0 means no score (the
window is not full yet). Code c >= 1 stands for the score ``2 ** ((c - 1) / 10 - 10)``: code 1 is 2 ** -10
(about 0.001) and code 255 is 2 ** 15.4 (about 43 000). Scores outside that range saturate at the end codes.

The node encodes with the same formula in C (``firmware/core/motion_score.h``); the shared test vectors pin the two.
"""
from __future__ import annotations

import math
from typing import Optional

NO_SCORE = 0
STEPS_PER_OCTAVE = 10
MIN_LOG2 = -10           # log2 of the score code 1 stands for
MAX_CODE = 255


def encode_score(score: Optional[float]) -> int:
    """Score code for a score (None for no score). Rounds to the nearest code in log2, halves upwards."""
    if score is None:
        return NO_SCORE
    if math.isnan(score):
        raise ValueError("score is NaN")
    if score <= 0:
        return 1
    steps = STEPS_PER_OCTAVE * (math.log2(score) - MIN_LOG2) + 0.5
    return int(min(max(math.floor(min(steps, MAX_CODE)) + 1, 1), MAX_CODE))


def decode_score(code: int) -> Optional[float]:
    """The score a code stands for, or None for code 0."""
    if not 0 <= code <= MAX_CODE:
        raise ValueError(f"score code must be 0..255, got {code!r}")
    if code == NO_SCORE:
        return None
    return 2.0 ** ((code - 1) / STEPS_PER_OCTAVE + MIN_LOG2)
