# 0027. Transceiver links: adaptive baseline only, and a room state from zones and independent links

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #8 (eberhart441/shadowcast), spec #2 ("Detection, room state, heatmap, track")
- **Supersedes:** [0009](0009-fixed-thresholds-by-default.md) for the transceiver pipeline only
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0011](0011-heatmap-evidence-from-quiet-baseline.md), [0020](0020-plug-and-play-no-per-layout-training.md), [0022](0022-host-time-round-broadcast-seam.md)

## Context

The transceiver rig of spec #2 has N(N-1) links: 870 at 30 nodes. Two things from the hackathon do not survive that.

- **Per-link thresholds.** [0009](0009-fixed-thresholds-by-default.md) made fixed per-link thresholds the default and
  auto mode experimental. A fixed threshold needs an empty-room calibration of every link after the last receiver
  reset; 870 of them is not an operator step, and [0020](0020-plug-and-play-no-per-layout-training.md) rules out a
  per-layout ritual. 0009 reverted auto mode because it detected walking less sensitively; it recorded no figures.
- **The room rule.** `RoomDecision` (`csi/adaptive.py`) reads MOTION when any two links are elevated together, or one
  is strongly elevated. Report 08 (`docs/research/scaling-redo/08-software-redo.md`, section 2) shows why that breaks:
  with a per-link chance elevation p, "any two of N" fires about C(N,2)p² of the time, 6 pairs at 4 links and
  378,015 at 870. Raising the fixed count costs sensitivity where few links see a person. And the links of one node
  fail together (a front-end glitch, a gain step, a reboot), so its 58 links are one witness, not 58.

## Decision

**Adaptive baseline only.** Every transceiver link is judged against its own adaptive baseline (`AdaptiveDetector`,
unchanged: 75 s median and robust spread, enter at max(baseline + 5.5 spread, 2 baseline) for 0.6 s), whatever the
monitor's mode. A transceiver link takes no threshold: `set_threshold` raises, `POST /api/threshold` answers 400. The
hackathon line pipeline keeps 0009 (fixed by default).

**Room state from zones and independent links** (`csi/room_state.py`, `RoomState`):

- The room is cut into zones of about 1 m (`A1` top-left), each with probe points every 0.25 m. A link covers a probe
  point when its segment passes within 0.3 m of it: a person there would raise it (the simulated falloff is 0.25 m).
- Links that share a node are not independent, and both directions of a node pair are one link. A set of links holds
  as many independent links as its largest subset with no shared node (a greedy matching; a lower bound). k faulty
  nodes give at most k independent links, however many links they are on.
- At each probe point p: capacity(p) = independent links among the warm links covering p; need(p) =
  max(2, min(3, capacity(p)), ⌈capacity(p)/3⌉). The point fires when the elevated links covering it hold need(p)
  independent links. A point with capacity below 2 has no independent view and never fires; a rig with no two
  independent links at all (2 or 3 nodes) needs one.
- A zone is MOTION while one of its points fires and for 1 s after; the room is MOTION while any zone is. WARMUP with
  no warm link. The single "strong link" path of `RoomDecision` is dropped for transceiver links: one link, however
  high, cannot be told from a fault of one of its two nodes.
- The payload gains `room.zones` (geometry) and `status.room.zones` ({zone: state}), for transceiver rooms only.

## Which way the trade-off goes (ADR-0001)

A miss costs more than a false alarm, so every knob was set towards MOTION as far as the acceptance criteria allow:

- **The required share is low: a third.** A person raises nearly every link covering the point they stand on, so a
  third costs little sensitivity and still removes most chance agreement. A half was not needed for the stated
  false-alarm rate, so it was not taken.
- **The need follows the links that can see.** Dead, silent and warming links leave the capacity, so a degraded rig
  needs less agreement, not the same.
- **The floor stays at two, and three only where three exist.** A fixed three everywhere missed people in sparse parts
  of the 8-node room (Evidence).
- **Almost no new delay.** Links already wait 0.6 s; the room re-judges its points at most once a round (50 ms) and
  keeps the 1 s hold on the way out.
- **Accepted cost: one node's view alone does not raise the room.** This is what "one noisy node does not raise the
  room" means geometrically. Where only one node's links pass (next to a corner node of a sparse rig, where its links
  fan out and no other node's link comes within about 0.75 m), a person reads exactly like that node's fault. They are
  reported once they step into another node's link: in the 8-node room 1.0 to 1.15 s after arriving, against 0.7 s
  elsewhere. We take that because the alternative, letting one node raise the room, makes a single bad board hold the
  whole room at MOTION, which hides real motion as surely as a miss. In the 30-node room (nodes 0.93 m apart) no such
  spot was found: 0.7 s at every tested point, corners included (Evidence).
- **Residual false alarms are accepted, not driven to zero:** about 1.5 an hour at the harsh glitch rate below.

## Alternatives considered

- **Two independent links at a point, never more** (the same zones, no step to three). Measured at the decision
  (random elevated links in the 30-node room): two chance-elevated links raise the room 22 % of the time, against
  2.3 % with the rule above, and it sees no position the rule above misses. Rejected.
- **A fixed three everywhere.** Quieter in the 30-node room (three chance links: 2 % against 5.7 %), but 4 of 108
  positions there and 62 of 108 in the 8-node room were not seen by three independent links, against 0 and 20 with
  the rule above. Rejected (misses).
- **A need from a binomial bound,** smallest k with C(n,k)qᵏ under a budget, for an assumed per-link chance rate q.
  Principled, but q has never been measured; a third of the capacity needs no such number. Revisit with recordings.
- **Ignore the ends of links** (a link covers only points 0.4 m or more from its two nodes). About twice as quiet in
  the 30-node room (two, three, four chance links: 1 %, 2 %, 6 % against 2.3 %, 5.7 %, 13 %) with no position lost
  there, but positions next to nodes were lost in the 8-node room: 26 unseen of 108 against 20. Not adopted (misses);
  worth another look once real rigs are denser than 8 nodes.
- **Sum per-link evidence over the zone** (report 08) or threshold the tomographic image (#10). Both need an evidence
  scale tuned on data, and would tie the room state to the heatmap's rework. Possible later; the zone ids stay.

## Consequences

- No calibration of any kind for transceiver links; ADR-0009's revert reason (walking sensitivity of auto mode) is
  still unmeasured on hardware. The 8-node labelled walk (#20) is where it gets measured.
- A transceiver room's state is per zone, which the possibly-occupied hold (#9) latches on.
- Two tests that raised only the two directions of one node pair now read EMPTY: that is one witness.
- At 870 links the old room decision rebuilt every link's reading per round broadcast and took most of the host CPU;
  the room state updates incrementally (about a tenth of the pipeline's time in the profile).

## Evidence

All simulated (`csi/simulate.py`: per-link lognormal noise, Gaussian 0.25 m falloff, plus the new `noisy` and
`glitch_rate_hz` options); no hardware. Tests in `tests/test_room_state.py` and `tests/test_round_pipeline.py`.

- **The 30-node room** is the frozen fixture `tests/fixtures/room_tr30.json` (from #11): 8 x 6 m, 30 nodes about
  0.93 m apart round the walls, 870 links, 48 zones.
- **Chance agreement, 30 nodes, 4 simulated hours** at the room state's input, every link glitching on its own once an
  hour (0.4 s elevated each, about 3400 glitches): the two-link rule reads MOTION about 75 times an hour, the room
  state about 1.5 (4 to 8 in 4 h over 4 seeds). Test bound: over 50 against under 3.
- **Empty 30-node room through the pipeline,** 180 s with every link glitching once per 2 h (1.5 s bursts): room EMPTY
  throughout, while two links were elevated together during the run.
- **One noisy node** (n07, all 58 links x8): at least 50 of its links MOTION, room EMPTY.
- **Walker onset,** a person appearing at grid points, 0.3 m from the walls at the edge (corners included): 30-node
  room 0.7 s at all 24 points; 8-node room 0.7 s at 11 of 15, 1.0 to 1.15 s at the 4 corner points.
- **Decision-level sweep** (g links elevated at random; a person at one of 108 grid positions modelled as raising every
  link within 0.45 m of them, or 0.3 m for the lower sensitivity). 30-node room: g = 2, 3, 4 chance links raise the
  room 2.3 %, 5.7 % and 13 % of the time (the two-link rule: always); no position unseen at 0.45 m, 2 at 0.3 m.
  8-node room: 14 %, 33 %, 60 %; 20 of 108 positions unseen at 0.45 m (30 at 0.3 m), the same as with a floor of two
  and no step to three: a sparse rig's blind spots come from its geometry, not from this rule. The simulated walker
  (its links respond out to about 0.55 m, and it moves) is seen at every tested point.
- **Not measured:** real glitch rates, how correlated real links are beyond a shared node, and the miss rate on
  hardware.
