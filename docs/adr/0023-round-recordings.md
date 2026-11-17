# 0023. Round recordings: the undecoded bytes, host time and source, in one versioned binary file

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #6 (eberhart441/shadowcast), spec #2 ("Recording, replay, simulator")
- **Related:** [0018](0018-evaluate-by-recording.md), [0022](0022-host-time-round-broadcast-seam.md), [0012](0012-host-ts-line-stream-seam.md)

## Context

ADR-0018 evaluates detection by replaying labelled recordings, never by random window. The hackathon's
recordings are CSV files of parsed packets, one label per file, and its raw log is text lines. The transceiver
pipeline has no packets on the host: its input is `(host time, round broadcast bytes)` per source (ADR-0022), up
to about 600 round broadcasts per second at 30 nodes. Later tickets (the room state, the heatmap, the rig
stages) need recordings to judge algorithm changes on real data, including runs with several gateways.

## Decision

- A round recording is one binary file (format in [`docs/round-recording.md`](../round-recording.md)): a
  magic, a u16 format version, a JSON header (room config, source names, notes, start time), then records of
  kind, host time, source index and body.
- It stores each round broadcast **as the source delivered it**, undecoded, rejected bytes included, with the
  source's host time and name. Replay yields the same pairs per source through the input seam, so decoding,
  rejection counting, de-duplication across gateways and everything after them run on the replay exactly as
  live. Tested: a simulated 8-node run replays frame for frame to the live run's link states and room state.
- Labels are records in the same file, each holding from its host time until the next, with optional extras
  (a floor mark). One file holds a whole run with several labels, unlike the CSV recordings.
- The header keeps the room config the run used, because the bytes name nodes only by serial.
- A reader refuses a version it does not know, and any kind of record its version does not define. A last
  record cut short is left out and counted, so a killed recorder still leaves a usable file.
- Replay is read-only, paces the recorded gaps at 1x, Nx or no waiting, and runs every link in auto mode.

## Alternatives considered

- **Extend the CSV recordings.** Rejected: there are no packets to tabulate, and one file per label splits a
  run whose labels change while it runs.
- **A text log like `--raw-log` (hex per line).** Rejected: about twice the size at 30 nodes, and the spec asks
  for a binary container.
- **Record decoded link reports.** Rejected for the reason of ADR-0022: the bytes are the contract, and a
  replay must exercise the decoder, rejection counts and de-duplication.
- **Skip unknown record kinds.** Rejected: a reader that ignores records drops data without a word; adding a
  kind bumps the version instead. Unknown header keys are ignored, since they carry no data the pipeline uses.

## Consequences

- A recording replays only through the transceiver pipeline; the hackathon recordings keep their own replay.
- `load_rounds` reads the whole file into memory (about 340 MB per hour at 30 nodes). A streaming reader can
  come later without changing the format.
- Labels set live are stamped with the wall clock, which matches host time only for wall-stamped sources (a
  gateway, the real-time simulator). A non-real-time source must pass the label's host time.
- `dashboard.py --record` records `--simulate` only until the gateway source lands; adding it is one more
  stream to tee.

## Evidence

- `tests/test_round_recording.py`: the round trip, the live-versus-replay equivalence on an 8-node simulated
  run (with a dead node and lossy links) both in `DashboardState` and through the dashboard server's run logs,
  a two-gateway run that replays with the same duplicate counts per gateway, pacing at 1x, 4x and full speed,
  and the refusals.
- No recording from real hardware exists yet.
