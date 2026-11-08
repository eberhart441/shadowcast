# 0016. Compute a finished calibration off the server lock

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `74e4eab` ("Compute a finished calibration off the server lock")
- **Related:** [0010](0010-two-noisy-room-calibration-methods.md), [0015](0015-stdlib-http-server-loopback-commands.md)

## Context

The dashboard's state sits behind one lock: the pump thread takes it to feed packets and emit frames, and HTTP handlers take it for `/api/state` and for commands. When a calibration recording ends, the report re-scores every clip of every link (`csi/links.py`, `calibrate_links_report`). That ran inside the lock, so every request and every frame waited for it.

## Decision

- `CalibrationJob` takes a `defer(compute, complete)` hook (`csi/live_calibration.py`).
- When the recording ends, the job enters `processing` and stops taking packets. The server runs `compute` on its own thread without the lock, then takes the lock only to apply the thresholds in `complete` (`csi/server.py`, `_defer_calibration`).
- The final frame of a run waits, without the lock and for at most 30 s, for a calibration that is still processing, so the result is in it (`_join_calibration`, `CALIBRATION_JOIN_S`).

## Alternatives considered

- **Compute inline under the lock** (the behaviour before `74e4eab`). Rejected: it stalls frames and requests for the length of the computation.

## Consequences

- Frames and requests keep flowing while the report is computed.
- Correctness rests on one invariant: `compute` touches no job state, and nothing appends to the clips once the job is `processing`. `csi/live_calibration.py` documents it.
- One more thread to reason about at shutdown, handled by the bounded join.

## Evidence

- **The motivation, in the commit:** "The report was computed while the server held its state lock, so every request and frame waited for it" (`74e4eab`).
- **Pinned by tests** (`tests/test_dashboard_controls.py`): `test_state_requests_are_answered_while_a_calibration_is_processing` holds the computation open and requires `/api/state` to answer, in state `processing`, within 15 s; `test_a_run_that_ends_while_a_calibration_is_processing_ends_with_it_done` requires the final frame to carry the finished calibration.
- **Not measured:** how long the stall lasted on the rig. The change was made from reading the code, not from a profile.
