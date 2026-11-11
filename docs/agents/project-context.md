# Project context

Read this first when you start work here. It says what the project is for, where it stands, why it is built the way it is, and what bites. It points into other docs for detail and does not restate them: `docs/README.md` indexes them all, `GLOSSARY.md` defines the terms, `docs/adr/` holds the decisions, `docs/agents/hardware.md` has the rules for boards, ports and thresholds, and `docs/agents/implementer-brief.md` says how to branch, check and land work. The rig itself (nodes, positions, sender addresses, board serials) is in `config/room.json`; read it there. Anything a command can look up (issues, test counts) is given here as the command, not its output.

## What this is for

A system for firefighters and spec ops to tell whether people are in an area, using WiFi channel state information (CSI) from small ESP32 boards. Boards at fixed points send and receive packets; a person moving between them changes the signal; the software scores each sender-receiver pair (a **link**) for motion and draws a rough heatmap on a web dashboard.

What that means for any decision you make:

- **A missed person is the costly error; a false alarm is cheap.** When a tradeoff pits fewer false alarms against fewer misses, say which way you went and why. Do not default to quieter output.
- **Motion is the reliable output. Stillness is not.** Breathing detection (STILL_PRESENT) false-fires on empty rooms and is off by default. A person who stays still for about 30 s fades into the baseline. The README's "Decision support, not a guarantee" note and its "Limits" are the authoritative wording: decision support, never the only check that a room is empty.
- **The room is never empty** in practice, so detection and calibration must tolerate noise and people.
- **What exists today is a bench prototype:** a 2.77 x 1.84 m room, four boards, one moving person, zone-level accuracy. Do not describe it as more than that.

## Where things stand

- **Branches:** the hackathon pipeline (everything under "How the system fits together") is on `main`. The old hackathon branches are kept as `archive/*` (for example `archive/scaling-and-accuracy`, `archive/turn-taking`, `archive/data-two-board-sessions`); `docs/research/scaling-redo/00-synthesis.md` maps each old name to its archive branch. They are history to read, not branches to build on. New work follows the brief's "Branch flow": a work branch, the integration branch the task names, and a pull request to `main`.
- **The redo:** spec #2 (`gh issue view 2`) plans scaling the rig to many nodes; its research is in `docs/research/scaling-redo/`, starting at `00-synthesis.md`. Its tickets are the sub-issues of #2.
- **Open issues:** `gh issue list --state open`.
- **The rig, beyond `config/room.json`:** the receivers are wireless and forward to a gateway board on the dashboard host's USB (see `hardware.md`, "Wireless receivers"); the senders run from battery or USB; the live thresholds are in the newest `data/links_cal*.json`, which is not in git (`data/` is gitignored).
- **Check a link before trusting its threshold:** in the 2026-11-07 calibration `rx1<-01` dropped to about 10 packets/s with RSSI -57; on 2026-11-08 every link ran at 37 to 40 packets/s. Look at the packet rate and drops on the link's card.
- **Receivers are wireless** since 2026-11-08: `docs/adr/0019-wireless-receivers-over-esp-now.md` supersedes the "wired for now" record 0004.
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

The seam that matters most: every source, serial, simulated, replayed or recorded, is a stream of `(host_ts, line)`. Anything that produces that stream plugs in without touching the pipeline, which is how a future UDP receiver would. The transceiver pipeline of spec #2 has its own seam, `(host time, round broadcast bytes)` ([ADR-0022](../adr/0022-host-time-round-broadcast-seam.md), format in [`docs/round-broadcast.md`](../round-broadcast.md)); `python scripts/dashboard.py --simulate --room config/room_tr8.json` runs it with no hardware. The README's "How it works" and `docs/architecture.md` give each stage; read them before changing one.

Where to start, by task:

| Task | Start here |
|---|---|
| Change detection, thresholds or calibration | `csi/detect.py`, `csi/links.py`, `csi/noisy_calibration.py`, `csi/live_calibration.py`, `docs/operations.md` "Calibrating" |
| Understand or change the detection math | `docs/theory.md` |
| Change the dashboard or its API | `csi/server.py`, `csi/dashboard.py`, `web/index.html`, `docs/operations.md` "Running live" |
| Module map, HTTP API, concurrency, test strategy | `docs/architecture.md` |
| Why something is built the way it is | `docs/adr/README.md` |
| Operator detail: calibrating, flags, troubleshooting | `docs/operations.md`, `docs/hardware.md` |
| Touch boards, ports or firmware | `docs/agents/hardware.md`, `firmware/README.md`, `scripts/ports.py`, `scripts/flash.sh` |
| Work without hardware | `python scripts/dashboard.py --simulate-room` (README "Quick start") |
| Change the room or add a node | `config/room.json`, `csi/room.py`, `docs/hardware.md` "Scaling up" |

## Glossary

The project's terms are defined once, in `GLOSSARY.md`.

## Decisions and why

Each decision, with the alternatives weighed and the evidence, is an ADR; start at `docs/adr/README.md`.

## What bites

- **Receivers re-baseline on reset.** Opening a port on a native-USB board, any `esptool` call, flashing and replugging can all reset one. Never run `esptool` (even `chip_id`) against a receiver while the dashboard runs. Do not restart the dashboard casually: set thresholds after the last restart, then leave it running. Use `scripts/ports.py` (it opens nothing) to identify boards.
- **USB port names change on every replug.** Identify boards by serial.
- **Boards flash from one build directory per chip and role.** The example's `sdkconfig` is shared and carries the last target; see `firmware/README.md`.
- **A flashed sender must have a unique `SENDER_ID`.** Two senders with the same id merge into one link and look like one noisy sender.
- **Duplicate or ghost senders:** a receiver hears every `1a:00:00:00:00:xx` sender, including a board you forgot to switch off. Count packets per sender before assuming a link is yours.
- **The dashboard holds the serial ports.** Another reader (`idf.py monitor`, `record.py`, `telemetry.py`) on the same port starves it.
- **Tests can time out on a loaded machine.** Calibration tests poll the live server. Check the load average before blaming code, and kill any busy-loop processes left by earlier load tests.
- **Python:** use a virtualenv with `requirements.txt` and `pip install -e .` installed, as in README "Quick start" (`<venv>/bin/python` on macOS and Linux, `<venv>\Scripts\python.exe` on Windows). `.venv` is gitignored, so a new worktree has none: point at an existing virtualenv by absolute path, including as `PYTHON=` to `scripts/check.sh`. CI runs Python 3.12.
- **macOS specifics:** bash 3.2 (an empty array under `set -u` is an error), `zsh` does not word-split unquoted variables, `timeout` is not installed, the sandbox refuses `PYTHONPATH`, use `/dev/cu.*` ports.
- **Windows specifics:** 33 tests need bash or POSIX signals and fail or do not import on native Windows: `tests/test_flash_script.py` (26), `tests/test_guards.py` (1), `tests/test_dashboard_cli.py` (3) and `tests/test_record_keyboard.py` (3; it needs `termios`, and without `-n` its import error stops collection, so pass `--ignore=tests/test_record_keyboard.py`). A red fast tier on Windows is not necessarily your change: compare the failures against those files, or rely on CI (Ubuntu). `scripts/check.sh` needs Git Bash and auto-detects only `.venv/bin/python`, so run it as `PYTHON=<venv>/Scripts/python.exe scripts/check.sh`.
- **Never `git switch` in the main checkout.** `data/` is gitignored on `main` and tracked on `archive/data-two-board-sessions`, so switching there deletes it. Use a worktree for every other branch, and move a branch pointer with `git branch -f` or `git -C <checkout> merge --ff-only` instead.

## Doing the common things

- **Check your work:** `scripts/check.sh` (ruff and the fast tests) before committing; `scripts/check.sh --full` before a merge. CI runs the full suite on Ubuntu for every pull request. Count the tests with `python -m pytest --collect-only -q | tail -1`. The fast tier includes `tests/test_docs.py`, which lints Markdown math and relative links, so it catches a broken doc edit too.
- **Run the dashboard with no hardware:** `python scripts/dashboard.py --simulate-room`.
- **Run it on the rig:** `python scripts/dashboard.py --auto-ports`, then calibrate (`scripts/calibrate.py`, or the page's Calibrate button).
- **Flash a board:** `scripts/flash.sh --dry-run ...` first; it checks the port's chip before writing and refuses a receiver while a dashboard answers on :8000.
- **Land work:** follow `docs/agents/implementer-brief.md`.
- **Anything that resets a receiver, flashes a board, pushes, merges or deletes** is the lead's call unless your task says otherwise.

## Doc drift to know about

- `config/room_c5_only.json` and some test fixtures carry `port_hint`, which nothing reads.
- The `--margin` help text in `csi/common.py` says the default 1.25 "is the 99th percentile itself"; a margin of 1.0 is the plain p99.
- The `QuietBaseline` docstring in `csi/heatmap.py` says "low percentile", but it defaults to the median (`BASELINE_PERCENTILE = 50.0`).
- `requirements.txt` lists `scipy` and `scikit-learn`, which nothing imports.
- `web/ui-overhaul/index.html` requests `GET /api/floorplan`, which the server does not implement (the page falls back to the plain room); its comments cite a `csi/floorplan.py` that does not exist.
