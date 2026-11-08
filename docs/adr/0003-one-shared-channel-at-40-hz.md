# 0003. One shared channel (6), senders at 40 Hz

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `fb3198a` (README: 40 Hz and the 100 Hz loss), `6975e7e` (firmware vendored with both settings)
- **Related:** [0002](0002-esp32-c5-receivers-only.md), [0004](0004-wired-receivers-wireless-deferred.md), [0005](0005-vendor-the-firmware.md)

## Context

Every receiver must hear every sender, so all boards share one channel. Senders are uncoordinated: each runs a free loop of `esp_now_send` then `usleep(1000 * 1000 / CONFIG_SEND_FREQUENCY)` (`firmware/csi_send/main/app_main.c`), so more senders or a higher rate means more collisions. The detector needs enough packets per link: a score needs at least 5 packets in its 1 s window, and calibration refuses a link that delivers under 15 packets/s (`csi/detect.py`).

## Decision

- **One fixed channel, 6:** 2.4 GHz, 40 MHz wide (HT40, secondary channel below), ESP-NOW at MCS0. It is hard-coded as `CONFIG_LESS_INTERFERENCE_CHANNEL` in both firmwares; the menuconfig channel alone does not change it.
- **Each sender transmits at 40 Hz** (`CONFIG_SEND_FREQUENCY`).
- With a third sender, lower every sender to about 30 Hz and reflash (README "Limits").

## Alternatives considered

- **The stock 100 Hz send rate.** Tried and rejected: two senders lost about 65% of packets.
- **Coordinated sending (TDMA slots after a sync beacon), then a multi-channel split after narrowing to HT20.** Proposed, not built: issues #16, #17, #19, #20 and #21 are open. Issue #16 notes that 65% loss is more than naive channel-utilisation arithmetic predicts, and suspects phase-correlated collisions between free-running senders. That hypothesis is unconfirmed; the proposed first experiment is the same two senders at 100 Hz with and without TDMA.
- **Which channel.** Channel 6 was picked, not surveyed. No channel scan or interference survey is recorded; the first README said to pick one, "e.g. 6", and keep it fixed (`2cc5131`).

## Consequences

- Time resolution and sender count trade directly against each other. A new sender means reflashing every sender at a lower rate.
- At 40 Hz a 1 s window holds 36 to 38 packets on a healthy link. Windows, filters and drop estimates run on device time, so drops shrink a window's packet count without stretching it (`csi/detect.py`, `csi/health.py`).
- One channel means a forgotten board with a `1a:00:00:00:00:xx` address joins the rig as a ghost sender. Count packets per sender before trusting a link.
- Wireless receivers would put their uplink on this same channel ([0004](0004-wired-receivers-wireless-deferred.md)).
- The simulator defaults to 100 Hz (`csi/common.py`, `SIM_RATE_HZ`), not the rig's rate; the dashboard and calibration estimate the rate from device time on real ports.

## Evidence

- **Two senders:** about 65% loss at 100 Hz, 5% to 10% at 40 Hz (README "Flashing the firmware" and "Scaling up"; first written in `fb3198a`).
- **Current rig:** four links at 36 to 38 packets/s with about 5% loss (README "The hardware rig").
- **A known weak link:** `rx1<-01`, the long diagonal, fell to about 10 packets/s at RSSI -57 during the last calibration (`docs/agents/project-context.md`).
- **Not measured:** three senders at any rate. The 30 Hz advice is an extrapolation.
