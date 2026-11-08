"""Thresholds for a room that is never empty, from live link scores. Pure function, standard library only.

    results = noisy_room_thresholds({"rx1<-00": [41.2, None, 39.8, ...]})
    results["rx1<-00"].threshold    # a float, or None with .reason set

The formula (docs/agents/hardware.md, "Thresholds in a noisy room"):

    threshold = max(median + K x 1.4826 x spread, FLOOR x median)

where ``spread`` is the lower-half spread: the median of (median - score) over the usable scores at or below
the median. Movement sits in the upper tail, so it reaches the estimate only through the median, never through
the spread. ``None`` scores (no score yet) are skipped and counted as empty readings.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

K = 6.0                  # how many robust standard deviations above the median
MAD_SCALE = 1.4826       # MAD to standard deviation for normal data
FLOOR = 3.0              # threshold is never below FLOOR x median
MIN_USABLE_FRACTION = 0.5
MIN_SCORES = 40


@dataclass(frozen=True)
class LinkThreshold:
    readings: int                    # every reading, None included
    empty: int                       # readings with no score
    median: Optional[float]          # over usable scores; None when there are none
    spread: Optional[float]          # lower-half spread
    max: Optional[float]
    threshold: Optional[float]       # None when the link was refused
    reason: Optional[str]            # why the link got no threshold

    @property
    def usable(self) -> int:
        return self.readings - self.empty


def link_threshold(scores: Sequence[Optional[float]]) -> LinkThreshold:
    usable = [float(s) for s in scores if s is not None]
    readings = len(scores)
    empty = readings - len(usable)
    if not usable:
        return LinkThreshold(readings, empty, None, None, None, None, "no usable readings")
    median = statistics.median(usable)
    spread = statistics.median([median - s for s in usable if s <= median])
    reason = None
    if len(usable) < MIN_USABLE_FRACTION * readings:
        reason = f"only {len(usable)} of {readings} readings usable (need at least half)"
    elif len(usable) < MIN_SCORES:
        reason = f"only {len(usable)} usable readings (need {MIN_SCORES} or more)"
    threshold = None if reason else max(median + K * MAD_SCALE * spread, FLOOR * median)
    return LinkThreshold(readings, empty, median, spread, max(usable), threshold, reason)


def noisy_room_thresholds(scores_by_link: Mapping[str, Sequence[Optional[float]]]) -> dict[str, LinkThreshold]:
    """Per-link result for ``{link label: scores}``, in the order given."""
    return {link: link_threshold(scores) for link, scores in scores_by_link.items()}
