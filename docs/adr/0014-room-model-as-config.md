# 0014. The rig is configuration: `config/room.json`

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `b182962` ("Add room and node layout config"), `447346d` ("Derive the node layout from config/room.json")
- **Related:** [0006](0006-identify-boards-by-usb-serial.md), [0011](0011-heatmap-evidence-from-quiet-baseline.md), [0017](0017-tests-pinned-to-frozen-fixture.md)

## Context

The rig changed constantly during the hackathon: chips were swapped between roles, and a third receiver was added, moved, changed chip and switched off in about an hour. Positions drive the heatmap geometry and the simulator; sender addresses and board serials tie packets and ports to nodes. Issue #9 asked that "moving a board only needs a config edit".

## Decision

- `config/room.json` describes the rig as data: the room rectangle (metres, origin top-left, y down), every node (role, chip, x, y, sender address, board serial, `enabled`) and per-link heatmap weights.
- Links are derived, never listed: every enabled receiver paired with every enabled sender.
- `"enabled": false` removes a node completely: no links, nothing in the payload, its port never opened. A node can be defined before its board is plugged in.
- Adding a board is a config entry, a flash and a recalibration, with no code change. `--room` selects another layout, and the `CSI_ROOM` environment variable overrides the default path.

## Alternatives considered

- **Node constants in code**, the state before `447346d`. Rejected; the simulator keeps its old constants only as a fallback when it is given no room.

## Consequences

- `config/room.json` is the source of truth for the rig. Prose that copies it drifts: the README's hardware table still calls `tx0` an ESP32-S3; the config says ESP32-C5.
- Tests must not read the live config, or every rig edit changes test outcomes ([0017](0017-tests-pinned-to-frozen-fixture.md)).
- Links grow as receivers × senders (3 × 2 = 6, 3 × 3 = 9). The shared channel limits growth before the config does ([0003](0003-one-shared-channel-at-40-hz.md)).
- Stale keys survive in old layouts: `port_hint` remains in `config/room_c5_only.json` and the test fixtures, and nothing reads it.

## Evidence

- **Eight rig changes after `447346d`, each a commit touching only `config/room.json`:** `c3334f4` (15:09, enable S3 `rx3`), `6a8598d` (15:15, move it beyond the top wall), `5c705c9` (15:51, `rx1` to C5, `rx3` off), `6b0bab3` (15:59, `rx3` back as a C5), `5fc978c` (16:04, onto the top wall), `c3220bc` (16:14, `rx3` off), `33dbc43` (21:47, `rx2` to S3), `7cc6184` (22:07, `rx2` and `tx0` to C5).
- **Scaling checked without hardware:** `python scripts/links_live.py --simulate-links --room tests/fixtures/room_5node.json` lists the six links of a three-receiver layout (README "Scaling up").
