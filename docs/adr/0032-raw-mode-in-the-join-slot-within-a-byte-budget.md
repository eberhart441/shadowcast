# 0032. Raw mode: a host-signed request sent in the join slot, raw CSI appended to round broadcasts within a byte budget

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #17 (eberhart441/shadowcast), spec #2 (user stories 34, 35, 36; "Raw mode" in Implementation Decisions)
- **Related:** [0015](0015-stdlib-http-server-loopback-commands.md), [0023](0023-round-recordings.md), [0027](0027-round-scheduler.md), [0028](0028-hmac-sha256-tag-and-round-number-freshness.md)

## Context

The transceiver rig sends a 4-byte link report per link and no CSI; algorithm work and recordings still need the full
CSI of a chosen link now and then. The spec asks for a raw mode: switched on for one link (or a few, within a
per-round byte budget) from the dashboard or a command, the request reaching the target node "through the gateway's
own slot", the node appending binary CSI to its round broadcasts. The round scheduler (#14) has no gateway slot: a
round is one slot per member and a join slot. Every frame on the channel must be signed with the kit key and carry a
round number that only counts forward (#16), and a gateway holds no kit key.

## Decision

- **Two frame types in format v2** (no layout change to the others, as #14 did for its join frames): 4 = raw mode
  request, 5 = round broadcast with raw CSI. A request names the target node and its transmitters by board serial and
  carries the whole raw set (0 to 2 links), so copies and repeats are harmless. A round broadcast with raw CSI is a
  round broadcast plus one or two blocks of int8 I/Q (up to 128 bytes, one HT20 long training field, as the driver
  gives it).
- **The host signs, the gateway only sends.** The host builds and signs the request (it has the kit key), with the
  gateway's serial as node id and its own count as round number, one up per frame. The gateway gets it over USB
  (`send <hex>`) and sends it verbatim. A target takes a request only within 64 rounds after its target round (the
  target's own round number), so a recorded request cannot be replayed later even after the 2 s resync of #16.
- **Sent in the join slot of even rounds.** The gateway follows the schedule from slot 0's round broadcasts and sends
  in the slot joiners already use, in rounds the scheduler numbers even, never in a member's slot nor in a grant's
  round, at most one frame per round, never early. A collision with a joiner costs one try on each side; the host
  sends three copies and the raw mode flag in the target's link reports is the acknowledgement.
- **Budget:** 133 bytes per raw link per round, at most 400 bytes per round in the rig (three links) and two links per
  node; the host refuses more with a reason. On the node, raw CSI only goes out if the frame still fits its slot
  (`rs_frame_cap`); otherwise it is dropped and counted, never sent late. By report 03's model the full budget costs
  about 1 % of the channel at 30 nodes and 20 Hz and 2 % at 16 nodes and 40 Hz. Frames over 250 bytes use ESP-NOW v2.
- **Recorded as is.** Raw CSI rides in round broadcasts, so round recordings (0023) keep it unchanged and replay it.
- **Commands:** `POST /api/raw-mode`, under ADR-0015's rules (loopback by default, `--allow-control` otherwise, JSON,
  Host and Origin checks, refused in replays). The page's link detail has a switch.

## Alternatives considered

- **A gateway slot added to every round.** The spec's wording; it would change the scheduler's slot count, capacity
  and every node's timing for a frame sent a few times per session. The join slot is already reserved for nodes
  that are not members. Ruled out for the cost.
- **The gateway holds the kit key and signs.** Every gateway would need provisioning and a freshness counter that
  survives restarts. The host already has the key. Ruled out.
- **Raw CSI in a separate frame after the round broadcast.** Two sends per slot, a slot that must hold both, and a
  frame that shares the round broadcast's (node, round) key for de-duplication. Ruled out.
- **Keep every frame inside ESP-NOW v1's 250 bytes** (half the subcarriers per round, or no raw CSI past ~26
  nodes). Every node runs the same image, which speaks v2. Ruled out.

## Consequences

- The scheduler takes the airtime of the frame it heard (not the membership's broadcast size) for timing, so a node
  streaming raw CSI keeps correct slot lateness.
- Joins can collide with raw mode requests in the join slot; joins retry. Requests are rare (an operator action).
- Raw mode lives in RAM: a node that restarts stops streaming; the host shows "no answer" if it never acknowledged.
- The CSI is not gain-compensated; algorithm work must handle gain (#15).
- Not measured on hardware: ESP-NOW v2 frames from the CSI callback, the gateway's join-slot timing. Rig check:
  `docs/rig-checks/raw-mode.md`.

## Evidence

- Airtime: `csi/raw_mode.py` (`ppdu_us` reproduces report 03's HT20 MCS0 table: 106, 406, 1198 µs), tested in
  `tests/test_raw_mode.py`.
- Join-slot sending: `firmware/core/gateway_tx.{h,c}`, `tests/test_gateway_tx.c`; a node streaming raw CSI in the
  simulated radio: `tests/test_round_scheduler.c` (`a_node_streaming_raw_csi_keeps_its_slot_and_its_timing`).
- End to end on the host: `tests/test_raw_mode_server.py` (command, recording, replay, refusals).
