# 0021. Every transceiver source is a `(host time, round broadcast)` stream

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #5 (eberhart441/shadowcast), spec #2 ("Host input seam")
- **Supersedes:** [0012](0012-host-ts-line-stream-seam.md) for the transceiver pipeline only
- **Related:** [0013](0013-packet-time-not-wall-clock.md), [0017](0017-tests-pinned-to-frozen-fixture.md), [0019](0019-wireless-receivers-over-esp-now.md)

## Context

In the transceiver rig of spec #2 no node prints CSI. Each node scores its links on the board and packs one
link report per link into the round broadcast it sends in its slot; gateways only listen and hand the host
each round broadcast with a receive time. The host never sees a CSI_DATA line, so the `(host_ts, line)` seam of
[0012](0012-host-ts-line-stream-seam.md) has nothing to carry. The pipeline still has to take data from a
gateway, the simulator and replayed recordings, and stay testable with no hardware.

## Decision

- Every source in the transceiver pipeline is an iterable of `(host time, round broadcast bytes)`
  (`BroadcastStream`, `csi/rounds.py`): a host timestamp in seconds, taken once when the bytes arrive, and the
  round broadcast exactly as a node sent it ([`docs/round-broadcast.md`](../round-broadcast.md)). A quiet
  source may yield `(host time, None)` as a heartbeat.
- The bytes are decoded on the host, in one place (`RoundIngest`, fed by `DashboardState.push_broadcast`).
  Bytes that fail to decode, round broadcasts from serials not in `config/room.json`, and link reports that
  cannot be placed yet are counted by reason, never raised and never dropped silently.
- A round broadcast names its sender by board serial inside the bytes (the node id field), so a stream needs no
  side channel for identity and a link is named by full node identity (`n1<-n5`), not by the last byte of an
  address.
- The simulator emits real round broadcast bytes, so simulated runs exercise the same decoder as the hardware.
- Sources still run through `MultiSource` and the dashboard server unchanged: bytes are queued undecoded and
  merged with other sources in host-time order. Host time keeps the meaning of [0013](0013-packet-time-not-wall-clock.md).
- The `(host_ts, line)` stream of [0012](0012-host-ts-line-stream-seam.md) stays, for the hackathon rig and its
  recordings.

## Alternatives considered

- **Keep the line seam and have the gateway print text.** Rejected: text formatting of CSI on the node is the
  first wall in the scaling research (report 01), and link reports are binary anyway.
- **Sources that yield decoded round broadcasts.** Rejected for the same reason as parsed packets in 0012: raw
  bytes keep the decoder under test and the firmware's bytes as the contract, and rejection counting belongs in
  one place.
- **Identity from the ESP-NOW source address, passed beside the bytes.** Rejected: it makes the seam a triple,
  a recording would have to store it separately, and the authentication tag cannot cover it.

## Consequences

- A new kind of source (the USB gateway, replay of recorded round broadcasts) is one more generator.
- Two pipelines coexist until the line pipeline is retired: `DashboardState.push` for packets and
  `push_broadcast` for round broadcasts, sharing the link monitor, heatmap, track and server.
- A link report names its transmitter by node index only. The host learns each node's index from that node's
  own round broadcasts per schedule version, so reports about a node not yet heard from are counted as
  unplaced for the first round.

## Evidence

- Implemented and tested without hardware: `tests/test_round_broadcast.py` (golden bytes),
  `tests/test_round_pipeline.py`, `tests/test_simulate_rounds.py` and `tests/test_round_server.py` (an 8-node
  simulated rig, 56 links, through the payload and the HTTP interface).
- No round broadcast from real hardware exists yet; the firmware that sends them is a later ticket.
