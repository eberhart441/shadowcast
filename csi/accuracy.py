"""The guided accuracy run: marks, the protocol, and ground truth from its prompts.

One person follows spoken prompts (scripts/accuracy_run.py) while the dashboard records raw CSI
(--raw-log). The protocol is defined once here, so the runner, the analysis (scripts/accuracy_eval.py)
and the simulated dry run all agree on it.

Marks: a 3 x 3 grid in room metres (x from the left wall, y from the receivers' wall). The grid is
symmetric about the room centre on purpose: mark k and mark 10 - k are mirror twins (1-9, 2-8, 3-7, 4-6),
the pairs a point-symmetric layout confuses, so the run measures that confusion directly.

Protocol, in order:
  intro        everyone leaves the room
  calibrate    the dashboard's empty-room calibration (retried if a link is refused)
  empty        EMPTY_S of empty room: false tracks
  marks 1-9    "go to mark k" (TRANSIT_S), then "hold, keep moving on the spot" (HOLD_S): bias and spread
  still        at mark 5: STILL_S standing completely still, then MOVE_AGAIN_S moving: how the dot behaves
  walk         a mark is called every WALK_STEP_S; walk to it, arriving as the next is called: error and lag
  leave        LEAVE_S of empty room again: drift over the run

Ground truth is the prompt times. Settling time after each prompt is left out of scoring (HOLD_SETTLE_S),
and walking truth is a straight line between marks at constant speed, which a person only approximates:
both are reported as truth uncertainty, never hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

MARKS: dict[int, tuple[float, float]] = {
    1: (0.46, 0.40), 2: (1.385, 0.40), 3: (2.31, 0.40),
    4: (0.46, 0.92), 5: (1.385, 0.92), 6: (2.31, 0.92),
    7: (0.46, 1.44), 8: (1.385, 1.44), 9: (2.31, 1.44),
}
MARK_TOLERANCE_M = 0.15     # taped mark plus where the body centre is while moving on the spot

INTRO_S = 20.0
EMPTY_S = 60.0
TRANSIT_S = 10.0
HOLD_S = 20.0
HOLD_SETTLE_S = 3.0         # after "hold": not scored
STILL_TRANSIT_S = 10.0
STILL_S = 30.0
MOVE_AGAIN_S = 10.0
WALK_INTRO_S = 10.0
WALK_STEP_S = 3.0
LEAVE_S = 30.0
LEAVE_SETTLE_S = 8.0        # time to walk out before the final empty segment is scored
EMPTY_SETTLE_S = 2.0
WALK_ROUTE: tuple[int, ...] = (2, 3, 6, 9, 8, 7, 4, 1,      # perimeter, twice
                               2, 3, 6, 9, 8, 7, 4, 1,
                               5, 9, 5, 1,                  # the 1-9 diagonal: mirror twins
                               2, 3, 5, 7, 5, 3)            # the 3-7 diagonal
WALK_START = 1


def twin(mark: int) -> int:
    """The mirror image of a mark through the room centre."""
    return 10 - mark


@dataclass
class Segment:
    kind: str                         # "empty", "hold", "still", "move_again", "walk"
    t0: float                         # scored window, host (Pi) time
    t1: float
    mark: Optional[int] = None
    truth: Optional[Callable[[float], tuple[float, float]]] = None
    meta: dict = field(default_factory=dict)


def segments_from_labels(labels: Sequence[dict], offset: float = 0.0) -> list[Segment]:
    """Scored segments in host time (label time minus ``offset``) from the runner's label events."""
    ev = [dict(e, t=e["t"] - offset) for e in labels]
    segs: list[Segment] = []
    by = lambda name: [e for e in ev if e["event"] == name]  # noqa: E731

    for e in by("empty_start"):
        end = next((x["t"] for x in ev if x["t"] > e["t"] and x["event"] == "empty_end"), None)
        if end is not None:
            settle = LEAVE_SETTLE_S if e.get("final") else EMPTY_SETTLE_S
            segs.append(Segment("empty", e["t"] + settle, end, meta={"final": bool(e.get("final"))}))

    for e in by("hold"):
        end = next((x["t"] for x in ev if x["t"] > e["t"] and x["event"] == "hold_end" and x["mark"] == e["mark"]),
                   None)
        if end is not None:
            p = MARKS[e["mark"]]
            segs.append(Segment("hold", e["t"] + HOLD_SETTLE_S, end - 0.5, e["mark"], truth=lambda t, p=p: p))

    still = by("still_start")
    if still:
        s = still[0]["t"]
        move = next((x["t"] for x in ev if x["t"] > s and x["event"] == "move_again"), None)
        stop = next((x["t"] for x in ev if x["t"] > s and x["event"] == "still_end"), None)
        p = MARKS[5]
        if move is not None:
            segs.append(Segment("still", s, move, 5, truth=lambda t: p))
            if stop is not None:
                segs.append(Segment("move_again", move + HOLD_SETTLE_S, stop, 5, truth=lambda t: p))

    calls = by("walk_call")
    end = by("walk_end")
    if calls and end:
        times = [c["t"] for c in calls] + [end[0]["t"]]
        marks = [WALK_START] + [c["mark"] for c in calls]

        def truth(t: float, times=times, marks=marks) -> tuple[float, float]:
            # leg k: from marks[k] (reached at times[k]) to marks[k+1], arriving at times[k+1]
            for k in range(len(times) - 1):
                if times[k] <= t < times[k + 1]:
                    a, b = MARKS[marks[k]], MARKS[marks[k + 1]]
                    f = (t - times[k]) / (times[k + 1] - times[k])
                    return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])
            return MARKS[marks[-1]]

        segs.append(Segment("walk", times[0], times[-1], truth=truth,
                            meta={"calls": list(zip(times[:-1], marks[1:]))}))
    return sorted(segs, key=lambda s: s.t0)
