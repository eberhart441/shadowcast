# Hardware and live-rig notes

Reference for anything that touches boards, serial ports, firmware, thresholds or the running dashboard. The rig itself (nodes, positions, sender addresses, board serials) lives in `config/room.json`; read it there.

## Receivers re-baseline on reset

`csi_recv` takes its gain baseline from the first 100 packets after boot. A reset shifts every score on that receiver's links, so a threshold tuned before the reset is stale after it.

Resets come from: any `esptool` command (including `chip-id`), flashing, replugging, and (observed) opening a native-USB port when a dashboard starts.

- Set thresholds after the last restart, then keep the dashboard running. Change thresholds through the page or API (`Set`, `Save`).
- Find boards with `python scripts/ports.py` (no side effects). It matches each connected board to its node by USB serial (the base MAC) and flags boards that match no node and nodes whose board is not connected. Run `esptool` only against the one port of the board being flashed, taken from that listing.
- Port names change on every replug and after a reset, so never reuse one from an earlier run. Start receivers with `--auto-ports`: the dashboard finds each enabled receiver's port by its `serial` in `config/room.json`, and fails naming any receiver that is not connected. `--port NAME=PATH` still works for explicit ports.

## Receiver chip

Use ESP32-C5 receivers. ESP32-S3 receivers were much noisier (35-65% false MOTION in a quiet room against 4-16% for a C5 receiver), and an S3 third receiver cut the other receivers' packet rate. The original ESP32 was noisy as receiver and as sender. ESP32-S3 and ESP32-C5 senders both work.

## Firmware

The firmware lives in `firmware/` (stock `esp-csi` examples plus our edits). Build and flash commands, the `SENDER_ID` build flag and the pre-flash cautions are in `firmware/README.md`; read it before touching a board. ESP-IDF 5.5.6 stays outside the repo, at `~/esp/esp-idf`.

## Thresholds in a noisy room

The room is never empty, so there are two ways to set per-link thresholds on a running dashboard. Both are on the HTTP API; neither is a substitute for checking the result against the room.

**Formula: `scripts/calibrate.py`.** It polls `GET /api/state` about 4 times a second for `--seconds` (default 45) and sets nothing unless asked. Each poll gives each link one reading: its score, or an empty reading while the link is silent (NO_SIGNAL). Per link it computes `max(median + 6 x 1.4826 x lower-half-spread, 3 x median)`, where lower-half-spread is the median of `(median - score)` over the usable scores at or below the median. Movement mostly sits in the upper tail, and the spread ignores it, though it can still move the median. A link with fewer than half its readings usable, or fewer than 40 usable scores, gets no threshold and the script says why. The printed table shows samples, empty readings, median, spread, max and threshold per link.

    .venv/bin/python scripts/calibrate.py                       # report only, default http://127.0.0.1:8000
    .venv/bin/python scripts/calibrate.py --apply              # POST /api/threshold per link, then /api/thresholds/save
    .venv/bin/python scripts/calibrate.py --apply --skip-bad-links

`--apply` refuses, and changes nothing, while any link lacks a threshold, unless `--skip-bad-links` is given; links skipped that way keep whatever the dashboard has. The dashboard must stay running afterwards (see "Receivers re-baseline on reset").

**Dashboard Calibrate button (`POST /api/calibrate`).** It counts down, records `clips` clips of `seconds` each with a `margin`, and sets the thresholds itself; the page shows the result and any skipped links with reasons. The default `noisy: true` takes the p97 x margin over every window and does not refuse for movement. `noisy: false` applies the quiet-room rules and refuses a clip that is mostly movement.

**Evidence, 2026-11-07.** After a calibration with the formula, the user recalibrated with the dashboard Calibrate button. For link rx2<-00 (an ESP32-S3 receiver at that time, with a noisy diagonal) the button gave 50.2 and the formula gave 137.0. The button's settings for that run (clips, margin, `noisy` mode) were not recorded. That is one link on one run. It does not show which threshold is right: neither was checked against labelled motion.

**What each suited, as far as this shows.** The formula needs no countdown, so it can sample a room that cannot be cleared; it reports whether each link had enough usable readings. The button records fixed clips after a countdown; with `noisy: false` the room must be clear enough for its quiet-room rules, and it reports skipped links with reasons. On the one noisy-diagonal link above, the button's threshold was lower. Whether lower is better is open.

## Git

Stay on `csi-sensing` in the main checkout and use `git worktree` for side branches. `data/` is gitignored on `csi-sensing` and tracked on `data/two-board-sessions`, so switching branches in the main checkout deletes it.
