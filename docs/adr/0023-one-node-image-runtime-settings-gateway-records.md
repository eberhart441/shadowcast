# 0023. One node image: runtime settings over USB, binary gateway records, HT20 MCS0 broadcasts

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** eberhart441/shadowcast issue #13 (spec #2), branch `ticket/13-two-nodes`
- **Related:** [0005](0005-vendor-the-firmware.md), [0006](0006-identify-boards-by-usb-serial.md), [0019](0019-wireless-receivers-over-esp-now.md), [0021](0021-on-node-score-streaming-window-and-score-code.md), [0022](0022-host-time-round-broadcast-seam.md)

## Context

The hackathon rig built one firmware per chip and role (`csi_recv`, `csi_send`, `CSI_FORWARD`, `CSI_GATEWAY`) and
one build directory per sender (`SENDER_ID`). Its receivers formatted every CSI sample as an ~850-byte text line
inside the Wi-Fi task, echoed it to a UART nobody read, and the wireless receivers re-sent each line as an
acknowledged unicast on the sensing channel at HT40 (reports 01 and 03). Spec #2 asks for one image for every board,
with role and settings at runtime, a listen-only gateway that writes binary records with a receive time to USB, and
unacknowledged HT20 MCS0 broadcasts. Ticket #13 is the first image that does this, for two transceivers and a
gateway; the round scheduler (#14), gain control (#15) and authentication (#16) come later.

## Decision

- **One image, `firmware/node`, for ESP32-C5.** No build flag changes what a board does. Its identity is the board
  serial (base MAC), read at boot; the round broadcast carries it.
- **Runtime settings in NVS, set over USB by text commands.** `role` (transceiver or gateway), `channel`, `index`,
  `nodes`, `round_hz` (`firmware/core/node_settings.{h,c}`; `scripts/node.py`). A set is all or nothing, saves and
  restarts the node. A transceiver that was never set only listens, so a fresh board cannot take a slot another
  board already has.
- **The USB port carries only framed binary records** (`docs/gateway-record.md`): sync `SC`, version, type, u16
  length, body, CRC-16/CCITT-FALSE. Heard records (receive time, RSSI, the frame body) on a gateway; status records
  (counters since boot) and text records (log lines, command replies) on every node. Log output is routed into text
  records; the console UART gets nothing after boot. The host reader resynchronises on the sync bytes and counts
  every rejected record and skipped byte.
- **The gateway forwards every ESP-NOW frame body it hears, undecoded.** The host decodes it in one place
  (ADR-0022) and counts what it rejects.
- **Sensing frames are ESP-NOW broadcasts at HT20, MCS0, long guard interval.** Unacknowledged, never retried. CSI is
  taken only from HT20 frames. The CSI callback copies the CSI, the frame body and the radio metadata into a queue;
  decoding, gain, amplitudes and scoring happen in the node task.
- **A fixed round for now** (`firmware/core/fixed_round.{h,c}`): `nodes` members, slot = node index, `round_hz`
  rounds per second, schedule version 1, no join slot. Timing comes from any heard round broadcast (the first sets
  the phase, later ones move it halfway). #14 replaces it behind the same calls.
- **Gain as the hackathon receiver did it** (record 100 frames, then force their median) until #15.

## Alternatives considered

- **Settings at flash time** (an NVS partition image written by esptool, as the shelved transceiver branch did with
  `flash_all.py`). Rejected: it ties every setting change to a flash, and it needs esptool on the board's port,
  which resets it.
- **Strap pins for the role.** Rejected for now: the XIAO has no spare strap, and the channel and slot need a
  runtime setting anyway.
- **Length-prefixed records without sync and CRC.** Rejected: the boot ROM prints text on the same USB port, and
  one lost byte would misalign every later record. The sync and CRC make a corrupt record cost only itself.
- **Text lines from the gateway** (as `RX,<serial>,...`). Rejected: text formatting of CSI on the node is wall 1 of
  the scaling research (report 01), and round broadcasts are binary.
- **Receive the round broadcast through the ESP-NOW receive callback on transceivers.** Not used: the shelved
  transceiver branch saw it stop firing a few seconds after CSI capture started, while the CSI callback kept
  working. The CSI callback has the frame body too, so one callback gives both the CSI and the bytes.

## Consequences

- Flashing one image to every board and running `scripts/node.py PORT set ...` replaces the per-board build
  directories. `scripts/flash.sh`, `SENDER_ID` and the `csi_send`/`csi_recv` builds stay for the hackathon rig only.
- Settings that must match across the kit (`channel`, `nodes`, `round_hz`) are set per board by hand until the
  scheduler (#14) takes membership and timing over. A mismatch shows as rejected or ignored broadcasts, not as an
  error.
- Reading a node's state needs `scripts/node.py`, not a serial terminal: the port carries binary records.
- The fixed round lengthens each round by about half the receive latency per round (the halfway correction), which
  is a fraction of a percent of the round rate.
- The CSI layout at HT20 on the C5 has not been seen on hardware: the node learns it (as `csi/parser.py` learns an
  unknown raw length) and counts frames it cannot score (`csi_skipped`).

## Evidence

- PC tests: `firmware/core/test.sh` (round broadcast and gateway record codecs against the shared golden fixtures,
  ESP-NOW body extraction, CSI layout, link table, fixed round with drift and latency, settings), and
  `tests/test_gateway_records.py`, `tests/test_round_server.py` and `tests/test_node_script.py` on the host side.
- The image builds with ESP-IDF 6.0 locally and in the CI job `node-image` (container `espressif/idf:v6.0`, pinned
  by digest).
- Nothing measured on hardware yet: the two-node rig check is `docs/rig-checks/two-nodes.md`.
