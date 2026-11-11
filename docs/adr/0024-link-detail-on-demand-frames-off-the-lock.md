# 0024. Per-link detail on demand, and frames rendered off the server lock

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** ticket #11 of spec #2 (branch `ticket/11-dashboard-scale`)
- **Related:** [0016](0016-calibration-off-the-server-lock.md), [0022](0022-host-time-round-broadcast-seam.md)

## Context

At 30 transceivers the rig has 870 links. Every frame carried one row per link and the page rebuilt one card per
link, five times a second. Report 02 (`docs/research/scaling-redo/02-host-pipeline.md`, §3 and §4.3) measured the frame
build plus `json.dumps` under the server lock at 29 ms and 55 KB per frame with 144 links; with 870 links a frame was
297 KB. The room decision looked at every link on every round broadcast (O(links²) per second).

## Decision

- **Frames of a room with more than 64 links (`FRAME_LINKS_MAX`) carry no `links` rows.** They carry
  `node_health`, `coverage`, `link_health` (aggregate) and `link_matrix` (one character per link) instead.
  `status.links_in_frames` says which kind a frame is. Smaller rooms (every hackathon rig, the 8-node stage) keep
  their rows, so logs and replays of them are unchanged.
- **`GET /api/link?id=<label>` returns one link's full row on demand.** `GET /api/state` still returns every row: it
  is one request, not a stream.
- **Payloads are built in two steps.** `DashboardState.capture(now)` advances the heatmap and track and takes
  references to fresh data (link rows, the grid, counters); it is the only step under the server lock.
  `DashboardState.render(capture)` and `json.dumps` run after the pump lets go of the lock. Only the pump thread
  emits frames, so they stay in order.
- **The room decision from link reports runs at most once per round** (`ROOM_STEP_S`, 50 ms of host time), and
  reading `room_state` or a snapshot brings it up to date.
- **Per-link statistics stop looping in Python per sample.** The adaptive detector keeps running counts for its
  rebuild test and a score-only deque for its median (same results); the heatmap's quiet baseline recomputes its
  percentile at most every 0.5 s of sample time (`BASELINE_RECALC_S`; a 60 s median barely moves in that time).

## Alternatives considered

- **Keep rows in frames but make them smaller** (short keys, fewer fields): still O(links) per frame in the pump
  and in the browser, and 870 rows is still tens of kilobytes. Ruled out.
- **Drop `links` from every frame**, small rooms included: simpler, but it changes the frames that hackathon logs,
  replays and evaluation scripts read. Ruled out for now; the 64-link cut keeps them.
- **A separate rendering thread**: not needed, since the pump renders after it releases the lock, and the GIL makes a
  second thread no faster.

## Consequences

- A replay of a large room has no per-link detail (`/api/link` answers 404 in a replay); the matrix still shows every
  link's state.
- `render` must read only the capture. Code added to the payload later must take what it needs in `capture`.
- The room decision can lag the newest round broadcast by up to one round (50 ms) inside the pump; against its 1 s
  hold this does not change when MOTION is reported.

## Evidence

Measured on the dev laptop (Core Ultra 7, Windows, Python 3.10), simulated 30-node rig, auto mode, steady state:

| | before | after |
|---|---|---|
| CPU per second of traffic | 1.8 s | about 0.5 s (0.62 with the machine loaded) |
| frame size | 297 KB | 16 KB |
| frame build (capture + render + JSON) | 24-52 ms | 6 ms |

`tests/test_dashboard_scale.py` holds the budget: under 1 CPU-s per second of traffic, under 25 ms per capture and
40 ms per frame, and under 40 KB per frame. A paced run of `scripts/dashboard.py --simulate --room
tests/fixtures/room_tr30.json` kept 5 frames/s at about 0.35 of a core. The Pi 5 was not measured.
