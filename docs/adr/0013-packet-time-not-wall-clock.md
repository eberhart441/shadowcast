# 0013. Pipeline time comes from the packets, not the wall clock

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `8565d3b` (score windows in device time), `404fe9e` (device counter events), `12bdc48` (receivers merged in host-time order)
- **Related:** [0012](0012-host-ts-line-stream-seam.md), [0017](0017-tests-pinned-to-frozen-fixture.md)

## Context

Several receivers are read on separate threads. Replays run at 1×, N× or as fast as possible, and tests run simulated streams much faster than real time. If detection read the clock at processing time, its results would depend on thread scheduling and replay speed. Each receiver also has its own clock, a 32-bit microsecond counter that wraps every 71.6 minutes and restarts when the board reboots.

## Decision

- **Host time.** Each line is stamped once, when it is read: a live port stamps the laptop's clock at read time; the simulator and replays stamp synthetic or recorded times. That `host_ts` travels with the packet and is the clock for link states, the debounce, NO_SIGNAL (2 s), the heatmap's baseline and smoothing, frame timing (one per 0.2 s), calibration clips and auto mode.
- **Device time.** The receiver's counter, unwrapped by `DeviceClock` (`csi/clock.py`), is the clock for the 1 s score window, the high-pass filter, send-rate estimates and drop counts. A reboot restarts the window instead of corrupting it.
- **Merge order.** The server releases packets from all receivers in host-time order (`csi/server.py`, `_Reorder`) before they reach the shared state.
- **The wall clock at processing time** is used only where a person or a socket is waiting: pacing the event stream (at most about 5 per second), the calibration countdown, noticing a stalled receiver (1 s; 2 s at start-up), and, for live wall-stamped sources only, moving time on between packets so a silent link turns NO_SIGNAL (`_clock`).

## Alternatives considered

- **The processing-time wall clock throughout.** Rejected: results would depend on thread timing and replay speed (`12bdc48`: packets are merged "so results do not depend on thread timing").
- **Device time throughout.** Unworkable across receivers: each board has its own counter, which restarts on reboot, so links on different receivers could not be compared or merged.

## Consequences

- A replay at any speed, and a seeded simulation, produces the same states; tests are deterministic and need no real-time waits (the event stream's pacing is switched off in `tests/conftest.py`).
- On live ports, host time is still the laptop clock at the moment a line is read, so USB and serial buffering shows up as timing jitter. The reorder buffer holds a receiver back by at most 1 s of wall time while it waits for a slower one.
- The terminal table (`scripts/links_live.py`) uses the wall clock for its display time. It is a live-only tool; the dashboard does not.

## Evidence

- **Stated in the code:** "All time is host time from the packets, never the wall clock" (`csi/heatmap.py`); "Time is packet time in seconds, never the wall clock" (`csi/adaptive.py`); the payload "is deterministic for simulated streams" (`csi/dashboard.py`).
- **Counter limits:** a 32-bit microsecond counter wraps about every 71.6 minutes and restarts on reboot; `DeviceClock` tells a wrap from a reboot by checking the step against elapsed host time (`csi/clock.py`).
- **The live exception is bounded:** time is advanced between packets only while the newest packet is within 5 s of the wall clock (`csi/server.py`, `WALL_CLOCK_TOLERANCE_S`). Replays of recorded sessions and simulations with synthetic time, which is what the tests use, never take it.
