# Hardware and live-rig notes

Reference for anything that touches boards, serial ports, firmware, thresholds or the running dashboard. The rig itself (nodes, positions, sender addresses, port hints) lives in `config/room.json`; read it there.

## Receivers re-baseline on reset

`csi_recv` forces its receive gain at packet 100: the median gain of the first 50 packets after boot, from all senders mixed. A reset picks a new gain and shifts every score on that receiver's links 3 to 5 times, so a threshold tuned before the reset is stale after it.

Resets come from: any `esptool` command (including `chip-id`), flashing, replugging, and (observed) opening a native-USB port when a dashboard starts. `serial_stream` now releases RTS before DTR as it opens a port, an order that never passes through the auto-reset state (DTR released while RTS is asserted), which should stop the last one; this is not yet confirmed on the rig, so until it is, treat a dashboard restart as a reset.

- Power every sender, reset the receivers in place, calibrate after the last reset, then keep every receiver and the dashboard running. Change thresholds through the page or API (`Set`, `Save`).
- A receiver's `agc_gain` / `fft_gain` (on its link cards, C5 receivers only) jumping to a new value and staying there usually means it reset: recalibrate its links. Whether a forced gain is reported unchanged is still to be checked on the rig.
- Find boards with `python scripts/ports.py` (no side effects). It matches each connected board to its node by USB serial (the base MAC) and flags boards that match no node and nodes whose board is not connected. Run `esptool` only against the one port of the board being flashed, taken from that listing.
- Port names change on every replug and after a reset, so never reuse one from an earlier run. Start receivers with `--auto-ports`: the dashboard finds each enabled receiver's port by its `serial` in `config/room.json`, and fails naming any receiver that is not connected. `--port NAME=PATH` still works for explicit ports.

## Receiver chip

Use ESP32-C5 receivers. ESP32-S3 receivers were much noisier (35-65% false MOTION in a quiet room against 4-16% for a C5 receiver), and an S3 third receiver cut the other receivers' packet rate. The original ESP32 was noisy as receiver and as sender. ESP32-S3 and ESP32-C5 senders both work.

## Packet budget

A receiver prints every packet from every sender as one text line (about 0.85 KB on a C5) at 921600 baud, which caps a C5 receiver at about 100-116 packets/s summed over all senders (an S3 at about 65-79). RF collisions are negligible at these rates. Keep the sum of the senders' real rates at or below about 80 packets/s per receiver:

- Senders default to 20 Hz, really about 19.6 packets/s, so up to 4 senders. Pick the rate from the number of senders a receiver hears: 2 -> `CSI_SEND_HZ=40` (38.5 real, about 77/s), 3 -> 25 (24.4 real, about 73/s), 4 -> 20 (about 78/s). The highest rate that fits leaves the most room for loss.
- Do not go below 20 Hz: calibration refuses a link under 10 packets/s or losing more than 60%, so a link can lose about 49% at 20 Hz, about 59% at 25 Hz and 60% at 40 Hz.
- For more senders, build the receivers with `CSI_PRINT_STRIDE=4` (about 3x shorter lines; the budget gain is an estimate, not yet measured).
- Judge loss by each link's `seq_loss` (from the sender's packet counter), not by `drops`, which assumes a send rate.

## Firmware

The firmware lives in `firmware/` (stock `esp-csi` examples plus our edits). Build and flash commands, the build flags and the pre-flash cautions are in `firmware/README.md`; read it before touching a board. ESP-IDF 5.5.6 stays outside the repo, at `~/esp/esp-idf`.

Build flags, set on the `set-target` line of a fresh build directory (`idf.py -B <dir> -D<FLAG>=<value> set-target <chip>`):

- `SENDER_ID` (sender, last byte of `1a:0c:12:00:00:<id>`; not `0xfe`), `CSI_SEND_HZ` (sender, default 20), `CSI_CHANNEL` (both, default 6, 5-11; 11 is the venue fallback).
- Receiver only: `CSI_PRINT_STRIDE` (default 1), `CSI_FORCE_GAIN` (default 1) with `CSI_PRINT_RAW` (default 0) for A/B recordings, and the experimental `CSI_GAIN_LOCK_STRONGEST` / `CSI_GAIN_LOCK_OFFSET`.
- Recalibrate after changing the stride or any gain flag.

**Reflash every board together.** Senders use the team prefix `1a:0c:12:00:00` plus `SENDER_ID`, receivers set their own address to `1a:0c:12:00:00:fe` and accept only that prefix, and `config/room.json` lists the new sender addresses. Boards on the old prefix (`1a:00:00:00:00:xx`) and the new one do not hear each other. Flash every sender and receiver from the same commit in one session, check the senders' boot lines (`mac: 1a:0c:12:00:00:<id>`) and the receivers' `CSI_DATA` addresses, then recalibrate. While boards are mixed, links go silent. Until the reflash, the dashboard counts an old sender address (`1a:00:00:00:00:XX`) as the configured sender with the same last byte, so a rig whose boards all still run the old firmware keeps working with the current laptop code.

## Thresholds after a scorer change

The motion scorer changed (2 s window, about 1 Hz high-pass), so thresholds files from before are on another scale (1.5-4x lower) and links read MOTION in an empty room. The dashboard, `links_live.py` and `detect_live.py` warn about a file made with another scorer, or with no scorer record (every older file, including those saved from the dashboard's Save button before it recorded the scorer). Recalibrate, and do it before saving from the dashboard: Save now records the live scorer for every threshold it writes. The quiet-room rules are the default, and the noisy-room p97 rule is opt-in.

## Thresholds in a noisy room

The room is never empty, so there are two ways to set per-link thresholds on a running dashboard. Both are on the HTTP API; neither is a substitute for checking the result against the room.

**Formula: `scripts/calibrate.py`.** It polls `GET /api/state` about 4 times a second for `--seconds` (default 45) and sets nothing unless asked. Each poll gives each link one reading: its score, or an empty reading while the link is silent (NO_SIGNAL). Per link it computes `max(median + 6 x 1.4826 x lower-half-spread, 3 x median)`, where lower-half-spread is the median of `(median - score)` over the usable scores at or below the median. Movement mostly sits in the upper tail, and the spread ignores it, though it can still move the median. A link with fewer than half its readings usable, or fewer than 40 usable scores, gets no threshold and the script says why. The printed table shows samples, empty readings, median, spread, max and threshold per link.

    .venv/bin/python scripts/calibrate.py                       # report only, default http://127.0.0.1:8000
    .venv/bin/python scripts/calibrate.py --apply              # POST /api/threshold per link, then /api/thresholds/save
    .venv/bin/python scripts/calibrate.py --apply --skip-bad-links

`--apply` refuses, and changes nothing, while any link lacks a threshold, unless `--skip-bad-links` is given; links skipped that way keep whatever the dashboard has. The dashboard must stay running afterwards (see "Receivers re-baseline on reset").

**Dashboard Calibrate button (`POST /api/calibrate`).** It counts down, records `clips` clips of `seconds` each with a `margin`, and sets the thresholds itself; the page shows the result and any skipped links with reasons. The default is now `noisy: false`: the quiet-room rules, which refuse a clip that is mostly movement. `noisy: true` takes the p97 x margin over every window and does not refuse for movement; a few seconds of movement in a clip can blind a link that way.

**Evidence, 2026-11-07.** After a calibration with the formula, the user recalibrated with the dashboard Calibrate button. For link rx2<-00 (an ESP32-S3 receiver at that time, with a noisy diagonal) the button gave 50.2 and the formula gave 137.0. The button's settings for that run (clips, margin, `noisy` mode) were not recorded. That is one link on one run. It does not show which threshold is right: neither was checked against labelled motion.

**What each suited, as far as this shows.** The formula needs no countdown, so it can sample a room that cannot be cleared; it reports whether each link had enough usable readings. The button records fixed clips after a countdown; with `noisy: false` the room must be clear enough for its quiet-room rules, and it reports skipped links with reasons. On the one noisy-diagonal link above, the button's threshold was lower. Whether lower is better is open.

## Git

Stay on `csi-sensing` in the main checkout and use `git worktree` for side branches. `data/` is gitignored on `csi-sensing` and tracked on `data/two-board-sessions`, so switching branches in the main checkout deletes it.
