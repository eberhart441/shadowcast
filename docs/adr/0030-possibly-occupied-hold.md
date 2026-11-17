# 0030. A zone that stops moving reads POSSIBLY_OCCUPIED for 60 s before EMPTY

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #9 (eberhart441/shadowcast), spec #2 ("Detection, room state, heatmap, track")
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0008](0008-motion-only-by-default.md), [0029](0029-adaptive-baseline-only-and-a-room-state-from-zones.md)

## Context

The score sees change, not presence ([0008](0008-motion-only-by-default.md), `docs/theory.md` section 11). A person
who stops dead leaves it within one to two seconds (the high-pass transient plus the 1 s window). A person who stays
in one place moving slightly keeps it raised until the adaptive baseline absorbs them, in about 30 s (24 to 30 s for
the rebuild rule; the README and `docs/agents/project-context.md` say "about 30 s"). Either way the zone of
[0029](0029-adaptive-baseline-only-and-a-room-state-from-zones.md) fell out of MOTION 1 s later and read EMPTY while
the person may still have been there. For a firefighter that is the costly error ([0001](0001-a-miss-costs-more-than-a-false-alarm.md)).
Nothing in the signal can tell a still person from an empty zone, so the only lever is how long the system keeps
saying "someone was just here".

## Decision

- A zone leaving MOTION (after its 1 s hold) reads **POSSIBLY_OCCUPIED** for the possibly-occupied hold, then EMPTY.
  Renewed motion in the zone makes it MOTION at once and restarts the hold from the end of that motion.
- The room reads MOTION while any zone is MOTION, else POSSIBLY_OCCUPIED while any zone is held, else EMPTY (WARMUP
  with no warm link, as before). A held zone stays POSSIBLY_OCCUPIED even if its links go silent: someone was seen
  there, which says more than "cannot see".
- **The hold is 60 s by default**, twice the ~30 s a still person takes to fade. It is a setting:
  `RoomState(possibly_occupied_s=...)`, `LinkMonitor`, `DashboardState`, `DashboardServer`, `rounds_dashboard`, and
  `dashboard.py --possibly-occupied-s SECONDS` (transceiver pipeline only; 0 turns the hold off). The payload reports
  it as `status.room.possibly_occupied_s`.
- The wording stays "decision support, not a guarantee": the page names the held zones and the hold, and says that a
  person who stands still is not seen once the hold ends.
- Transceiver rooms only. The hackathon line pipeline (`RoomDecision`) is unchanged.

## Why 60 s (ADR-0001)

- **At least the fade time.** A person who stops dead disappears from the score in about 1.5 s; one who fidgets in
  place disappears after about 30 s. A hold shorter than 30 s would clear the zone of someone who stopped dead
  sooner than the system would have kept seeing someone who did not quite stop. So 30 s is the floor.
- **Then towards fewer misses.** A miss costs more than a false alarm. A held zone that someone has in fact left is
  a cheap error: the state says "possibly", is drawn unlike MOTION, and a second look settles it. So the floor is
  doubled rather than met exactly.
- **Not longer.** Every zone a person crosses stays held for the whole hold, so a team moving through an 8 x 6 m room
  would hold most of its 48 zones. At several minutes the map would stop showing where anyone has been recently and
  read "possibly occupied" everywhere, which carries no information (the same reason breathing is off by default,
  [0008](0008-motion-only-by-default.md)). No recording measures how long a still person really stays; the 8-node
  labelled walk (#20) is where a still segment can be recorded.

## Alternatives considered

- **No hold** (the 1 s MOTION hold only, as in 0029). Rejected: a person who stops is reported EMPTY within about
  2.5 s.
- **A hold equal to the fade, 30 s.** Defensible, but takes the floor as the answer; rejected under 0001.
- **A latched state until the operator clears it.** Never false-negative for a person who stays, but every zone
  anyone ever crossed stays latched, so the state carries no information after the first minutes, and it needs an
  operator action that a plug-and-play kit avoids ([0020](0020-plug-and-play-no-per-layout-training.md)). Rejected.
- **Hold MOTION itself longer** instead of a separate state. Rejected: it hides whether there is motion now, which is
  the reliable output, and makes "motion detected within about a second" meaningless on the way out.

## Consequences

- A still person is reported for about 61 s after they stop (1 s MOTION hold plus the 60 s hold), then not at all.
  The README's limits still apply.
- The room state has a fourth value. Consumers that only knew MOTION, EMPTY and WARMUP must treat POSSIBLY_OCCUPIED
  as "not clear": `LinkMonitor.room_state` ranks it between MOTION and EMPTY; the default page draws it; the optional
  `web/ui-overhaul/` page does not know it yet (it reads the hackathon payload).
- Tests that walk a person from point to point now see the previous point's zones held before the next arrival.
- The simulator gained `WalkAndStop` (csi/room.py), a walker whose links fall over 1.5 s once they stop. `Walk` and
  `Stand` keep raising links while they are near them, like a person moving slightly in place.

## Evidence

All simulated; no hardware.

- **Walker who stops,** 8-node room (`tests/test_room_state.py`): MOTION while walking, POSSIBLY_OCCUPIED in B2 from
  4 s after the stop to the end of the hold (60 s, and 10 s when set), EMPTY afterwards although the simulated person
  is still standing there. The same walk with the position-only `Walk` stays MOTION, so the test depends on the stop.
- **Fade times** are the repo's, not new measurements: `docs/theory.md` section 11 (1 to 2 s for a person who stops;
  24 to 30 s for the rebuild rule), the README "Limits" (about 30 s).
- **Not measured:** how long a still person stays still in practice, and the hold's cost in false "possibly occupied"
  zones on a real rig.
