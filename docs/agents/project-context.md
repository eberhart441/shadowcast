# Project context

Read this first when you start work here. It says what the project is for, where it stands, why it is built the way it is, and what bites. It points into other docs for detail and does not restate them: `README.md` (usage, how it works, results), `docs/agents/hardware.md` (boards, ports, thresholds), `firmware/README.md` (building and flashing), `docs/agents/implementer-brief.md` (how to land work). The rig itself (nodes, positions, sender addresses, board serials) is in `config/room.json`; read it there.

## What this is for

A system for firefighters and spec ops to tell whether people are in an area, using WiFi channel state information (CSI) from small ESP32 boards. Boards at fixed points send and receive packets; a person moving between them changes the signal; the software scores each sender-receiver pair (a **link**) for motion and draws a rough heatmap on a web dashboard.

What that means for any decision you make:

- **A missed person is the costly error; a false alarm is cheap.** When a tradeoff pits fewer false alarms against fewer misses, say which way you went and why. Do not default to quieter output.
- **Motion is the reliable output. Stillness is not.** Breathing detection (STILL_PRESENT) false-fires on empty rooms and is off by default. A person who stays still for about 30 s fades into the baseline. The README's "Intended use and limits" is the authoritative wording: decision support, never the only check that a room is empty.
- **The room is never empty** in practice, so detection and calibration must tolerate noise and people.
- **What exists today is a bench prototype:** a 2.77 x 1.84 m room, four wired boards, one moving person, zone-level accuracy. Do not describe it as more than that.

## Where things stand (2026-11-07)

- All of issues #1-#15 (detection, calibration, dashboard, recording, replay, controls) are implemented, merged and closed. Issues #16, #17 and #19-#21 (TDMA and multi-channel) are open.
- `main` and `csi-sensing` are identical. `csi-sensing` is the working branch; work reaches `main` through a pull request (see the brief, "Landing on main").
- **The rig:** four ESP32-C5 boards at 40 Hz on channel 6. Two receivers on USB (top left `rx2`, top right `rx1`), two senders off USB (bottom right `tx0` with `SENDER_ID` 0x00, bottom left `tx1` with 0x01). A third receiver `rx3` is defined and disabled in `config/room.json`. Thresholds live in the newest `data/links_cal*.json`; `data/` is gitignored.
- **A known weak link:** `rx1<-01` (bottom-left sender to top-right receiver, the long diagonal) dropped to about 10 packets/s with RSSI -57 during the last calibration, so its threshold may be unreliable. Check packet rate and drops on its card before trusting it.
- **Receivers stay wired for now** (decided 2026-11-07). The wireless estimate is in "Decisions" below.
- **Not ours:** the remote branches `origin/fix/scaling-and-accuracy` and `origin/siv/turn-taking` belong to teammates. Leave them alone.
- **Little data:** the repo has two single-link recordings (branch `data/two-board-sessions`) and no labelled recording of the four-link rig, so there are no accuracy figures for the current rig. Say so rather than quoting numbers from the single-link recordings.

## How the system fits together

```
sender boards (ESP-NOW, ch 6, 40 Hz)
      -> receiver boards (measure CSI per packet, print CSI_DATA lines over USB serial, 921600 baud)
      -> csi/parser.py  (one amplitude vector per packet)
      -> csi/multisource.py  (one thread per receiver)
      -> csi/links.py + csi/detect.py  (per-link motion score, threshold, debounce)
      -> csi/heatmap.py + csi/dashboard.py  (heatmap and one JSON payload)
      -> csi/server.py  (HTTP: page, /api/state, SSE /api/events, commands)  -> web/index.html
```

The seam that matters most: every source, serial, simulated, replayed or recorded, is a stream of `(host_ts, line)`. Anything that produces that stream plugs in without touching the pipeline, which is how a future UDP receiver would. The README's "How it works" and "Modules" sections give each stage; read them before changing one.

Where to start, by task:

| Task | Start here |
|---|---|
| Change detection, thresholds or calibration | `csi/detect.py`, `csi/links.py`, `csi/noisy_calibration.py`, `csi/live_calibration.py`, README "Calibrating" |
| Change the dashboard or its API | `csi/server.py`, `csi/dashboard.py`, `web/index.html`, README "Running live" and "HTTP endpoints" |
| Touch boards, ports or firmware | `docs/agents/hardware.md`, `firmware/README.md`, `scripts/ports.py`, `scripts/flash.sh` |
| Work without hardware | `python scripts/dashboard.py --simulate-room` (README "Quick start without hardware") |
| Change the room or add a node | `config/room.json`, `csi/room.py`, README "Scaling up" |

## Glossary

- **Link:** one (receiver, sender) pair, labelled `<receiver><-<sender id>`, e.g. `rx1<-00`. Four links with two receivers and two senders.
- **Sender / receiver / node:** boards by role. A receiver measures CSI on packets from every sender whose address starts `1a:00:00:00:00`; the last byte is the sender id.
- **Score:** a link's motion measure: the variance of the high-passed amplitude over a 1 s window, averaged across subcarriers.
- **Threshold:** the score above which a link reads MOTION. One per link, from calibration or set by hand.
- **States:** EMPTY, MOTION, WARMUP (window not full yet), UNCALIBRATED (no threshold), NO_SIGNAL (no packet for 2 s). STILL_PRESENT is the experimental breathing state.
- **Calibration:** setting a link's threshold from a stretch of live scores. A **quiet-room** run refuses clips that are mostly movement; a **noisy-room** run accepts them and reports how much looked like movement. Calibration **margin** scales the threshold.
- **Gain baseline:** a receiver fixes its automatic gain from the first 100 packets after boot. **Re-baseline** means a reset shifted it, which makes every saved threshold on that receiver stale.
- **Heatmap:** a rough motion-likelihood map built from the links' scores and the room geometry. It shows where change is, not where a person is.
- **`SENDER_ID`:** the last byte of a sender's address, set at firmware build time.
- **Board serial:** a board's USB serial, equal to its base MAC (e.g. `38:44:BE:A5:18:B0`). It identifies a board; the `/dev/cu.usbmodem*` name does not, because it changes on replug.

## Decisions and why

- **ESP32-C5 receivers only.** S3 receivers were much noisier (35-65% false MOTION in a quiet room against 4-16% for a C5), and an S3 third receiver cut the others' packet rates. See `hardware.md`.
- **Fixed per-link thresholds are the default; `--auto-threshold` is experimental.** Adaptive detection (`csi/adaptive.py`) was less sensitive to walking.
- **Motion-only by default; breathing is opt-in.** It false-fires on empty rooms (43% of empty windows on hw5).
- **Thresholds for a noisy room.** The room is never empty, so there are two methods: a formula (`scripts/calibrate.py`) and the dashboard's Calibrate button. They disagreed on one link (rx2<-00: 137 vs 50) and neither was checked against labelled motion, so no winner is declared. See `hardware.md`.
- **Channel 6, 40 Hz.** Chosen for the shared channel; more senders mean more collisions (README "Scaling up").
- **Tests are pinned to a frozen four-node fixture** (`tests/fixtures/room_rig4.json`, via `CSI_ROOM` in `tests/conftest.py`), so reconfiguring the live rig never changes test outcomes. Tests that load the real config are the deliberate exception.
- **The server uses only the standard library**, and the dashboard commands are loopback-only by default (README "Security").
- **Firmware is vendored** in `firmware/` (stock `esp-csi` plus our edits) so the repo is self-contained.
- **Boards are found by serial**, not port name (`scripts/ports.py`, `--auto-ports`).
- **A calibration's report is computed off the server lock**, so requests and frames keep flowing while it runs.
- **Wireless receivers: not now (2026-11-07).** Estimated, never prototyped: forwarding over UDP works about 85-90% likely; detection quality holding without retuning only about 50-60%, because a receiver's own uploads would share channel 6 with the senders. A half-day spike would settle it and needs a router pinned to channel 6 and power for each receiver. Field use would also need a portable access point and authenticated or encrypted UDP.

## What bites

- **Receivers re-baseline on reset.** Opening a port on a native-USB board, any `esptool` call, flashing and replugging can all reset one. Never run `esptool` (even `chip_id`) against a receiver while the dashboard runs. Do not restart the dashboard casually: set thresholds after the last restart, then leave it running. Use `scripts/ports.py` (it opens nothing) to identify boards.
- **USB port names change on every replug.** Identify boards by serial.
- **Boards flash from one build directory per chip and role.** The example's `sdkconfig` is shared and carries the last target; see `firmware/README.md`.
- **A flashed sender must have a unique `SENDER_ID`.** Two senders with the same id merge into one link and look like one noisy sender.
- **Duplicate or ghost senders:** a receiver hears every `1a:00:00:00:00:xx` sender, including a board you forgot to switch off. Count packets per sender before assuming a link is yours.
- **The dashboard holds the serial ports.** Another reader (`idf.py monitor`, `record.py`, `telemetry.py`) on the same port starves it.
- **Tests can time out on a loaded machine.** Calibration tests poll the live server. Check the load average before blaming code, and kill any busy-loop processes left by earlier load tests.
- **macOS specifics:** bash 3.2 (an empty array under `set -u` is an error), `zsh` does not word-split unquoted variables, `timeout` is not installed, the sandbox refuses `PYTHONPATH`, use `/dev/cu.*` ports.
- **Python:** use `/Users/andrewaburustum/Documents/codemetalforge/.venv/bin/python`. CI runs Python 3.12; the local interpreter is newer.
- **Never `git switch` in the main checkout.** `data/` is gitignored on `csi-sensing` and tracked on `data/two-board-sessions`, so switching deletes it. Move a branch pointer with `git branch -f` or `git -C <checkout> merge --ff-only` instead.

## Doing the common things

- **Check your work:** `scripts/check.sh` (ruff and the fast tests) before committing; `scripts/check.sh --full` before a merge. The full suite (439 tests on 2026-11-08) takes under a minute on a quiet machine. CI runs it once per commit on a pull request.
- **Run the dashboard with no hardware:** `python scripts/dashboard.py --simulate-room`.
- **Run it on the rig:** `python scripts/dashboard.py --auto-ports`, then calibrate (`scripts/calibrate.py`, or the page's Calibrate button).
- **Flash a board:** `scripts/flash.sh --dry-run ...` first; it checks the port's chip before writing and refuses a receiver while a dashboard answers on :8000.
- **Land work:** follow `docs/agents/implementer-brief.md`.
- **Anything that resets a receiver, flashes a board, pushes, merges or deletes** is the lead's call unless your task says otherwise.

## Doc drift to know about

Fixed on 2026-11-08 (`docs/presentation-polish`): `GLOSSARY.md` and `docs/adr/` now exist; the README is a
short front page, and the operator detail moved to `docs/hardware.md` and `docs/operations.md`, which show
`tx0` as a C5, build from `firmware/` with `scripts/flash.sh`, and document `scripts/calibrate.py` and
`scripts/ports.py`. The math is in `docs/theory.md` and the engineering tour in `docs/architecture.md`.

Still open:

- `config/room_c5_only.json` and some test fixtures still carry `port_hint`, which nothing reads. The
  fixtures `room_5node.json` and `room_star.json` still list some nodes as ESP32-S3.
- `config/room.json` gives `rx1<-01` a heatmap weight of 0.3. It was set when `rx1` was an ESP32-S3; `rx1`
  is a C5 now, so the weight may no longer be warranted. Nobody has measured that.
- The `--margin` help text in `csi/common.py` says 1.25 "is the 99th percentile itself"; a margin of 1.0 is
  the plain p99. The `QuietBaseline` docstring in `csi/heatmap.py` says "low percentile" but defaults to the
  median. `csi/parser.py` hard-codes the C5 nulls (57-59) and otherwise ORs the first 20 packets, which the
  older README described as "locked from the first valid packet".
- `requirements.txt` lists `scipy` and `scikit-learn`, which nothing imports.
- `web/ui-overhaul/index.html` requests `GET /api/floorplan`, which the server does not implement (the
  page falls back to the plain room).
- 33 tests need bash or POSIX signals and fail on native Windows; CI runs on Ubuntu.
