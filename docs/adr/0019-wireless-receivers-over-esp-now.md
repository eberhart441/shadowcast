# 0019. Wireless receivers: forward CSI over ESP-NOW to one gateway

- **Status:** Accepted
- **Date:** 2026-11-08
- **Recorded in:** `165ce75`, `68c2b5f`
- **Supersedes:** [0004](0004-wired-receivers-wireless-deferred.md)
- **Related:** [0003](0003-one-shared-channel-at-40-hz.md), [0006](0006-identify-boards-by-usb-serial.md), [0012](0012-host-ts-line-stream-seam.md)

## Context

Wired receivers tie the dashboard host to cable reach and cost one USB port each ([0004](0004-wired-receivers-wireless-deferred.md)). Of the wireless options weighed there, the gateway needs no access point or other new infrastructure.

## Decision

Receivers run `csi_recv` built with `CSI_FORWARD=1` and send each CSI line over ESP-NOW on channel 6 to one gateway board (`csi_recv` built with `CSI_GATEWAY=1`) on the dashboard host's USB. The gateway prints `RX,<receiver serial>,<line>`; `scripts/dashboard.py --gateway PATH` splits that into one `(host_ts, line)` stream per receiver ([0012](0012-host-ts-line-stream-seam.md)) and names each receiver by its `serial` in `config/room.json` ([0006](0006-identify-boards-by-usb-serial.md)).

## Alternatives considered

- **Stay wired** ([0004](0004-wired-receivers-wireless-deferred.md)). Still supported: the dashboard takes `--auto-ports` or `--port` instead of `--gateway`, one source at a time.
- **UDP through an access point pinned to channel 6.** Not tried.

## Consequences

- The relayed lines share channel 6's airtime with the senders' packets ([0003](0003-one-shared-channel-at-40-hz.md)).
- Every receiver's serial must be in `config/room.json`; lines from any other serial are dropped without a message, so a replaced receiver looks dead until its serial is added.
- A receiver within about 1 m of the gateway loses its forwards: its gain locks about 1 s after boot, and the gateway's acknowledgements then swamp it. See `docs/agents/hardware.md`, "Wireless receivers".
- Wired and wireless receivers cannot be mixed in one dashboard.

## Evidence

On 2026-11-08, on the demo rig (two receivers, two senders, the gateway on a Raspberry Pi), all four links ran at 37 to 40 packets/s through the gateway. Per-link scores were not compared against the wired baseline on the same rig, so that half of 0004's revisit condition is still open.
