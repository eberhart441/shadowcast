# 0012. Every packet source is a `(host_ts, line)` stream

- **Status:** Accepted for the line pipeline (the hackathon rig and its recordings); superseded by [0021](0021-host-time-round-broadcast-seam.md) for the transceiver pipeline
- **Date:** 2026-11-07
- **Recorded in:** issue #1 ("Single stream seam"), `1b7e1a8` (`MultiSource`), issue #12 and `12bdc48` (the dashboard server keeps the seam)
- **Related:** [0004](0004-wired-receivers-wireless-deferred.md), [0013](0013-packet-time-not-wall-clock.md), [0017](0017-tests-pinned-to-frozen-fixture.md)

## Context

The pipeline has to take packets from live serial ports, from a simulator, from recorded sessions, and later from wireless receivers. It also has to be testable with no hardware attached, by people who do not have the boards.

## Decision

- Every source is a per-receiver iterable of `(host_ts, line)`: a host timestamp in seconds and one text line exactly as a receiver prints it.
- Sources are named (receiver name to stream) and read concurrently by `MultiSource`, each on its own thread with its own parser, so one receiver's malformed lines or layout warm-up never touch another's. A source that raises is reported in `status` and the others keep running.
- Two sanctioned variations. A quiet serial port yields a heartbeat, `(host_ts, None)`, so readers can check their stop flag. A replayed recorded session yields `(host_ts, CSIPacket)`, because its packets were parsed when they were recorded.
- The simulator renders real CSI_DATA text, so simulated runs exercise the same parser as the hardware.

## Alternatives considered

- **A separate code path per kind of source.** Rejected in issue #1: every tool consumes one stream shape and shares the parser's statistics.
- **Sources that yield parsed packets.** Used only where the packets already exist (recorded-session replay). Everywhere else, text keeps the parser under test and keeps the firmware's printed output as the contract.

## Consequences

- A new kind of receiver is one new generator. `MultiSource`, the per-link monitor, the heatmap and the server do not change; issue #12 required this so that "a wireless receiver can plug in later".
- The whole test suite drives simulated streams through public interfaces, with no hardware (414 test functions on this branch).
- Every line is parsed as text on the laptop. Malformed lines are counted and skipped, never fatal.
- One timestamp per line is taken at ingestion; what that timestamp means is [0013](0013-packet-time-not-wall-clock.md).

## Evidence

- **Sources that implement it today:** `serial_stream` (`csi/parser.py`), the looping simulated room (`csi/server.py`, `looping_room_streams`), the paced session replay (`csi/replay.py`, `_paced`); `scripts/dashboard.py` (`build_sources`) picks one per run.
- **The seam held under change:** the unmerged ESP-NOW gateway prototype (`feat/wireless-receivers`, `165ce75`) adds `csi/gateway.py`, which splits the gateway's single stream back into one stream per receiver, plus a `--gateway` option in `scripts/dashboard.py`. No other module under `csi/` changed in that commit.
