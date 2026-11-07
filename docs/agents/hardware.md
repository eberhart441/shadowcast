# Hardware and live-rig notes

Reference for anything that touches boards, serial ports, firmware, thresholds or the running dashboard. The rig itself (nodes, positions, sender addresses, port hints) lives in `config/room.json`; read it there.

## Receivers re-baseline on reset

`csi_recv` takes its gain baseline from the first 100 packets after boot. A reset shifts every score on that receiver's links, so a threshold tuned before the reset is stale after it.

Resets come from: any `esptool` command (including `chip-id`), flashing, replugging, and (observed) opening a native-USB port when a dashboard starts.

- Set thresholds after the last restart, then keep the dashboard running. Change thresholds through the page or API (`Set`, `Save`).
- List devices with `python -m serial.tools.list_ports -v` (no side effects). Run `esptool` only against the one port of the board being flashed.
- A board's port name can change after a reset. Re-list ports before reading it.

## Receiver chip

Use ESP32-C5 receivers. ESP32-S3 receivers were much noisier (35-65% false MOTION in a quiet room against 4-16% for a C5 receiver), and an S3 third receiver cut the other receivers' packet rate. The original ESP32 was noisy as receiver and as sender. ESP32-S3 and ESP32-C5 senders both work.

## Firmware

The firmware lives in `firmware/` (stock `esp-csi` examples plus our edits). Build and flash commands, the `SENDER_ID` build flag and the pre-flash cautions are in `firmware/README.md`; read it before touching a board. ESP-IDF 5.5.6 stays outside the repo, at `~/esp/esp-idf`.

## Thresholds in a noisy room

The room is never empty. Sample about 40 s of live link scores and set, per link, `max(median + 6 x 1.4826 x lower-half-spread, 3 x median)`, where lower-half-spread is the median of `(median - score)` over scores at or below the median. Movement only adds to the upper tail, so it cannot inflate the estimate. Apply with `POST /api/threshold`, then `POST /api/thresholds/save`.

## Git

Stay on `csi-sensing` in the main checkout and use `git worktree` for side branches. `data/` is gitignored on `csi-sensing` and tracked on `data/two-board-sessions`, so switching branches in the main checkout deletes it.
