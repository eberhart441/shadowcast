# 0005. Vendor the ESP32 firmware in the repository

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `6975e7e` ("Vendor the ESP32 CSI firmware into firmware/ with an overridable SENDER_ID"), `228fca6` (`scripts/flash.sh`)
- **Related:** [0002](0002-esp32-c5-receivers-only.md), [0003](0003-one-shared-channel-at-40-hz.md), [0006](0006-identify-boards-by-usb-serial.md)

## Context

The project started from Espressif's `esp-csi` get-started examples "as shipped" (issue #1, "Out of Scope"). The four-node rig needed five edits to them: channel 6 in a hard-coded define, a per-sender `SENDER_ID`, a receiver filter that accepts every `1a:00:00:00:00:xx` sender, a locked receiver gain, and a 40 Hz send rate. The README told each teammate to clone `esp-csi` and apply the edits by hand, and building two chips from one shared build directory once mixed up their targets.

## Decision

- Both examples live in `firmware/` (`csi_send`, `csi_recv`), taken from upstream `esp-csi` at commit `8633d67` with our edits applied and listed in `firmware/README.md`.
- `SENDER_ID` is a build parameter (`idf.py -DSENDER_ID=0x01`), not a source edit.
- One build directory per chip and role. `scripts/flash.sh` builds, checks that the port holds the named chip, and flashes one board; it refuses a receiver while a dashboard answers on port 8000.
- ESP-IDF 5.5.6 stays outside the repository.

## Alternatives considered

- **Stock examples as shipped** (issue #1's starting point). Rejected: the rig does not work without the five edits.
- **Clone upstream and apply the edits by hand** (what the README still describes). Superseded: every flash depended on someone redoing five edits correctly.

## Consequences

- The repository is self-contained, and the firmware is reviewable next to the parser that reads its output.
- Upstream fixes no longer arrive by themselves; re-vendoring is a deliberate step from the recorded upstream commit.
- Firmware and parser are coupled: changing the printed columns means changing `csi/parser.py` and its tests in the same change (`firmware/README.md`, "Serial output").
- `dependencies.lock` records the last `set-target` and must be reverted after building another chip (`firmware/README.md`).
- The README's "Flashing the firmware" section still describes the clone-and-edit flow (known drift, `docs/agents/project-context.md`).

## Evidence

- **The edits, in source:** `CONFIG_LESS_INTERFERENCE_CHANNEL 6` (both), `CONFIG_SEND_FREQUENCY 40` and the `SENDER_ID` default `0x00` (`firmware/csi_send/main/app_main.c`); `memcmp(info->mac, CONFIG_CSI_SEND_MAC, 5)` and `CONFIG_FORCE_GAIN 1` (`firmware/csi_recv/main/app_main.c`).
- **Why the gain lock matters, suggestively:** on the same boards and the current code, hw3 (gain not yet locked) reads walking as MOTION 80% of the time and hw5 (gain locked) 100% (README "Results and known limits"). The sessions differ in more than the gain lock, so this is not a controlled comparison.
