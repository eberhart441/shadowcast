# Operating the system

This guide covers running the system once the boards are in place: calibrating each link, running the
live dashboard and the terminal tools, recording and evaluating labelled sessions, and fixing common
problems. Setting up the boards, flashing them and finding their ports is in [hardware.md](hardware.md).

Run every command from the repository root, with the project's Python environment active (see the quick
start in the top-level [README](../README.md)). Placeholders such as `<PORT>` and `<PORT_RX1>` are serial
ports: find them with `python scripts/ports.py` (see
[Finding the serial port](hardware.md#finding-the-serial-port)). Every script prints its options with
`--help`; the [Command reference](#command-reference) lists them all.

## Contents

- [A session on the rig](#a-session-on-the-rig)
- [Calibrating](#calibrating)
  - [Before you calibrate](#before-you-calibrate)
  - [Choosing a method](#choosing-a-method)
  - [The dashboard Calibrate button](#the-dashboard-calibrate-button)
  - [The noisy-room formula script](#the-noisy-room-formula-script)
  - [Which method to trust](#which-method-to-trust)
  - [Calibrating from the terminal](#calibrating-from-the-terminal)
  - [Calibration rules](#calibration-rules)
  - [Calibration margin](#calibration-margin)
  - [Calibration tips](#calibration-tips)
  - [Setting thresholds from live data](#setting-thresholds-from-live-data)
  - [Keeping thresholds valid](#keeping-thresholds-valid)
- [Running live](#running-live)
  - [Dashboard on hardware](#dashboard-on-hardware)
  - [What the page shows](#what-the-page-shows)
  - [New UI](#new-ui)
  - [Motion track](#motion-track)
  - [Reading the heatmap](#reading-the-heatmap)
  - [Auto-threshold mode](#auto-threshold-mode)
  - [Dashboard options](#dashboard-options)
  - [HTTP endpoints](#http-endpoints)
  - [Security](#security)
  - [Terminal tools](#terminal-tools)
  - [Breathing](#breathing)
  - [Logging and replay](#logging-and-replay)
- [Recording and evaluating](#recording-and-evaluating)
  - [Recording a session](#recording-a-session)
  - [Session folders](#session-folders)
  - [Rehearsing without hardware](#rehearsing-without-hardware)
  - [Evaluating](#evaluating)
  - [Recorded data](#recorded-data)
- [Troubleshooting](#troubleshooting)
- [Command reference](#command-reference)

## A session on the rig

The short version of everything below:

1. Place the boards, power the senders, and plug each receiver into its own USB port
   ([hardware.md](hardware.md#the-rig)).
2. Run `python scripts/ports.py`. Every node should be matched, with no `NOT CONNECTED` line.
3. Reset each receiver in place (reset button, or unplug and replug) without moving anything.
4. Start the dashboard and open <http://127.0.0.1:8000/>:

   ```bash
   python scripts/dashboard.py --auto-ports --save-thresholds <THRESHOLDS_FILE>
   ```

5. Calibrate inside the running dashboard: the **Calibrate** button with the room empty, or
   `python scripts/calibrate.py --apply` when the room cannot be cleared. After the button, click
   **Save thresholds**; `calibrate.py --apply` saves for you.
6. Walk test: walk through the room and check that every link's score rises well above its threshold,
   and falls back below it when the room is still.
7. Leave the dashboard running. A restart can reset the receivers and make the thresholds stale.

The team keeps threshold files under `data/`, which is gitignored. Pass a saved file back with
`--thresholds` only if no receiver has reset since it was made.

## Calibrating

Each link has its own signal scale, so each link needs its own threshold. The dashboard uses **fixed
thresholds by default**, from a `--thresholds` file, a calibration, or a value set by hand. Without any of
these, a link shows `UNCALIBRATED`. `--simulate-room` calibrates itself on a simulated empty room, and the
experimental `--auto-threshold` mode needs no calibration (see [Auto-threshold mode](#auto-threshold-mode)).
The terminal tools (`links_live.py`, `detect_live.py`, `telemetry.py`, `live.py`) refuse to start on a real
port without a threshold, rather than guess.

### Before you calibrate

**Reset the receivers in place after placing the boards, before calibrating.** A receiver that booted
before the boards were placed keeps a gain baseline from the old set-up. Place the boards, reset each
receiver without moving anything (its reset button, or unplug and replug), then calibrate. Calibrate
after the last reset of any receiver; see [Keeping thresholds valid](#keeping-thresholds-valid).

### Choosing a method

| Method | Runs against | Room during calibration | Threshold | Result |
|---|---|---|---|---|
| Dashboard **Calibrate** button (`POST /api/calibrate`) | The running dashboard | Empty; or as it is, with **noisy room** ticked (the default) | Noisy room: margin x p97 over all windows. Quiet room: margin x p99 of the clean windows. | Applied live; **Save thresholds** writes the file. |
| `scripts/calibrate.py` | The running dashboard, over HTTP | As it normally is; no countdown | `max(median + 6 x 1.4826 x spread, 3 x median)` from live scores | Printed; `--apply` sets and saves them. |
| `links_live.py --calibrate` | The serial ports (dashboard stopped) | Empty | Quiet-room rules, margin x p99 | `links.json` for `--thresholds` |
| `detect_live.py --calibrate` | One serial port (dashboard stopped) | Empty | Quiet-room rules, margin x p99 | `calibration.json` with one threshold, for the single-link tools |
| **Set** on a link card (`POST /api/threshold`) | The running dashboard | Any | The value you type | One link; **Save thresholds** writes the file. |

On the rig, prefer the methods that run inside the dashboard. Starting the dashboard opens the receivers'
ports, which appears to reset them, so thresholds made beforehand by `links_live.py --calibrate` may be
stale by the time the dashboard uses them.

### The dashboard Calibrate button

In the page: **Controls, Calibrate on an empty room, Start calibration**. The run counts down ("Clear the
room"), records the clips, then sets each link's threshold itself. The page shows the result, and any
skipped link with the reason. **Cancel** stops a run.

| Setting | Default | Allowed |
|---|---|---|
| Clips | 3 | 1 to 10, whole number |
| Seconds per clip | 10 | 1 to 120 |
| Margin | 1.25 | any positive number |
| Countdown before the first clip (`lead_s`, API only) | 5 s | 0 to 60 s |
| Pause between clips | 3 s | fixed |
| **noisy room** | ticked | ticked or not |

With the defaults a run takes about 45 s. With **noisy room** ticked, movement in the clips is not
refused: the threshold is margin x p97 over all windows, and the result carries a warning with how much
looked like movement. Untick it for the quiet-room rules, which refuse a busy clip. Both are described in
[Calibration rules](#calibration-rules). The result becomes fixed thresholds for those links; click **Save
thresholds** to keep them.

The same run from the command line:

```bash
curl -X POST http://127.0.0.1:8000/api/calibrate -H "Content-Type: application/json" \
  -d '{"clips": 3, "seconds": 10, "margin": 1.25, "noisy": true}'
```

### The noisy-room formula script

The room is rarely empty, so `scripts/calibrate.py` computes thresholds from the live scores of a running
dashboard, with no countdown and no clips:

```bash
python scripts/calibrate.py                           # sample 45 s, print the table, change nothing
python scripts/calibrate.py --seconds 60              # a longer sample
python scripts/calibrate.py --apply                   # then set every link's threshold and save the file
python scripts/calibrate.py --apply --skip-bad-links  # apply the links that got a threshold; leave the rest
python scripts/calibrate.py --url http://127.0.0.1:8001
```

How it works (`csi/noisy_calibration.py`):

1. It polls `GET /api/state` about 4 times a second for `--seconds` (default 45). Each poll gives each
   link one reading: its score, or an empty reading while the link is silent (NO_SIGNAL) or has no score
   yet.
2. Per link, over the usable scores: `m` is the median, and the lower-half spread `s` is the median of
   `(m - x)` over the scores `x` at or below `m`. The threshold is:

   ```
   threshold = max(m + 6 x 1.4826 x s, 3 x m)
   ```

   Movement sits in the upper tail, so it does not inflate the spread, though it can still move the
   median.
3. A link gets no threshold, with the reason printed, when fewer than half its readings are usable or it
   has fewer than 40 usable scores.
4. It prints one row per link: samples, empty readings, median, spread, max and threshold.

`--apply` then sends `POST /api/threshold` for each link and `POST /api/thresholds/save`, which writes the
file the dashboard saves to (see [Setting thresholds from live data](#setting-thresholds-from-live-data)).
It refuses, and changes nothing, while any link lacks a threshold, unless `--skip-bad-links` is given;
skipped links keep whatever the dashboard has. Applied links become fixed, also in auto mode.

The script's commands are refused unless the dashboard accepts commands (see [Security](#security)), so
run it on the dashboard's machine. Exit status: 0 done (or report printed), 1 the dashboard could not be
reached or returned an error, 2 `--apply` refused. Keep the dashboard running afterwards.

### Which method to trust

No winner has been declared. On 2026-11-07 the formula and the Calibrate button were both run on the
rig, and on one link, `rx2<-00`, they disagreed: the formula gave 137.0 and the button gave 50.2. The
button's settings for that run (clips, margin, noisy mode) were not recorded. That is one link on one
run, and **neither threshold has been checked against labelled motion**, so it does not show which is
right. The details are in [`docs/agents/hardware.md`](agents/hardware.md), "Thresholds in a noisy room".

What each suits, as far as this shows:

- **The formula** needs no countdown, so it can sample a room that cannot be cleared, and it reports
  whether each link had enough usable readings.
- **The button** records fixed clips after a countdown. With **noisy room** unticked the room must be
  clear enough for the quiet-room rules, and it reports skipped links with reasons. On the one link
  above its threshold was lower; whether lower is better is open.

Whichever you use, finish with a walk test: every link's score should rise clearly above its threshold
while someone walks across it, and stay below it when the room is still.

### Calibrating from the terminal

The terminal calibrations open the serial ports themselves, so stop the dashboard first, and keep the
room empty:

```bash
# All links: three 10 s clips with 3 s pauses, pooled
python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --calibrate 10 --clips 3 --out links.json

# One link (single port)
python scripts/detect_live.py --port <PORT> --calibrate 60 --out calibration.json
```

- `links_live.py --calibrate SECONDS` records `--clips` clips (default 1) with 3 s pauses, scores each
  link on its own, and writes every link that calibrated to `--out` (default `links.json`). A link that
  could not be calibrated is listed as `NOT CALIBRATED` with the reason. The script exits with status 1
  if any link was skipped (the file is still written when at least one link calibrated), or if none
  calibrated (nothing written). Ctrl+C during the clips calibrates on what was recorded.
- Loss is measured against the sender's rate: `links_live.py --rate HZ` sets it. Left out, the rate is
  estimated from device timestamps, as the dashboard does.
- `detect_live.py --calibrate SECONDS` writes one threshold for one port to `--out` (default
  `calibration.json`), for `detect_live.py`, `telemetry.py` and `live.py`. Those tools treat a port as a
  single link; with two senders in the room they mix both senders' signals. Use `links_live.py` or the
  dashboard for the rig.
- Both use the quiet-room rules and take `--margin`.

### Calibration rules

The terminal tools always use the quiet-room rules. The dashboard's button uses the noisy-room rules when
**noisy room** is ticked (the default) and the quiet-room rules otherwise. `scripts/calibrate.py` uses
neither; it has its [own formula](#the-noisy-room-formula-script).

Quiet-room rules:

- Each clip is split into 1 s chunks, and chunks with movement in them are dropped, so a passer-by does
  not raise the threshold. A chunk counts as movement when its median score is more than 3 times the quiet
  baseline (the median of the lower half of the chunk medians), or when its 90th percentile is more than
  2.5 times its own median (a burst).
- A link needs at least 8 s of clean data, and at least half of the clip time must be clean. Otherwise
  that link gets no threshold, and the reason is printed ("input too short" or "only N s of M s was
  quiet").
- The threshold is `margin x p99`: the 99th percentile of the kept empty-room motion scores, times the
  margin (default 1.25).
- In the dashboard, a link whose clips were mostly rejected keeps its previous threshold and shows a
  warning.

Noisy-room rules (dashboard default):

- Movement is never a refusal. Every window counts, and the threshold is `margin x p97` over all windows
  (default margin 1.25).
- The result carries a warning with how many seconds looked like movement.

Both rule sets:

- Refuse a link that delivers under 15 packets/s or loses more than 60% of its packets.

### Calibration margin

The margin is the factor in `threshold = margin x p99` under the quiet-room rules, or `margin x p97`
under the noisy-room rules. Set it with `--margin` on `links_live.py`, `detect_live.py` and `evaluate.py`,
with the Margin field in the dashboard, or with `dashboard.py --margin` for a `--replay-session` run.
Lower margins catch brief or weak motion but raise false alarms; higher margins are quieter but may miss
weak motion. Measured on the single-link `hw5` session:

| Margin | Empty windows read as MOTION | Walking windows read as MOTION |
|---|---|---|
| 1.0 | 14% | 100% |
| 1.1 | 11% | 100% |
| **1.25 (default)** | **6%** | **100%** |
| 1.5 | 5% | 100% |

In this application a missed person costs more than a false alarm, so do not raise the margin only to
quiet the output. Compare margins on your own recordings, and raise the margin for a noisy room:

```bash
python scripts/evaluate.py --margin 1.0 data/hw5
python scripts/evaluate.py --margin 1.5 data/hw5
```

### Calibration tips

- Clear the room and keep still while the clips record. Stand outside the lines between boards.
- Several short clips beat one long clip: a passer-by then costs only one clip.
- A bursty link (heavy loss) may need a manual threshold: use the dashboard's **Set** button for that
  link, or edit its value in `links.json`.
- After calibrating, compare each threshold with that link's score while the room is empty. A threshold
  far above the empty score will miss motion; one far below it will false-alarm.

### Setting thresholds from live data

When a calibration refuses a link, or a clip looks wrong, set a threshold from live scores instead.
`scripts/calibrate.py` automates the method that worked on this rig (see
[The noisy-room formula script](#the-noisy-room-formula-script)). To do it by hand:

1. Sample about 40 s of each link's score. The `links` array of `GET /api/state` (or of one
   `/api/events` event) carries each link's `id` and `score`.
2. For each link, take `m`, the median of its samples, and `s`, the median of `(m - x)` over the samples
   `x` at or below `m`. Then `threshold = max(m + 6 x 1.4826 x s, 3 x m)`.
3. Apply each value with **Set** on that link's card, or by POST:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/threshold -H "Content-Type: application/json" \
     -d '{"link": "rx1<-00", "value": 12.3}'
   ```

4. Click **Save thresholds**, or `POST /api/thresholds/save`.

A Set makes that link's threshold fixed; the value must be a positive number. Saving writes every current
threshold, as `{label: number}`, to one file: the `--save-thresholds` path, else the `--thresholds` file,
else `links_thresholds.json` in the working directory. `GET /api/thresholds` shows the current thresholds
and that path. `--thresholds` reloads the file on the next start.

### Keeping thresholds valid

Thresholds do not survive a receiver reset. A receiver re-records its gain baseline whenever it resets,
which shifts its scores, so a threshold tuned before a reset is stale. Opening a port when the dashboard
restarts appears to reset the receiver as well. So:

- Set thresholds **after** the last restart of the dashboard and of every receiver, and do not restart
  the dashboard once you have tuned it.
- Never run `esptool` against a port that a dashboard is using.

## Running live

### Dashboard on hardware

```bash
python scripts/dashboard.py --auto-ports --thresholds links.json
python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json
```

Open <http://127.0.0.1:8000/>; if port 8000 is taken, add `--http-port 8001`. `--auto-ports` finds each
enabled receiver's port by its USB serial ([hardware.md](hardware.md#finding-receivers-automatically)).
With `--port`, name every port after its node: unnamed values become `rx1`, `rx2`, and so on in the order
given, which attaches a port to the wrong position if the order is wrong.

A real port started without `--thresholds` still runs, but its links show `UNCALIBRATED`, and
`status.messages` explains why; calibrate in the page. Ctrl+C (or SIGTERM) stops the server and closes
every port.

### What the page shows

The page is `web/index.html`, one static file with no network assets.

- **Room.** The nodes at their configured positions (receivers and senders drawn differently; a node
  outside the room is drawn with a dashed leader), the links coloured by state, and the heatmap. A caption
  says it is a rough motion-likelihood estimate, not a position track.
- **Links.** One card per link: state, score against threshold, packet rate, drops, RSSI, whether it
  contributes heat, and a 30 s score sparkline.
- **Status bar.** Receivers (connected, finished or failed), link counts by state, calibration state,
  packet time, and messages.
- **Controls.** The calibration panel (clips, seconds per clip, margin, **noisy room**, start, cancel;
  see [The dashboard Calibrate button](#the-dashboard-calibrate-button)), a threshold field and **Set**
  per link with **Save thresholds**, and heatmap sliders for sensitivity, decay, rise and falloff. The
  controls send commands, so they work only when the server accepts them (see [Security](#security)).
- **Pause** freezes the display while the server keeps running. The page reconnects by itself after a
  server restart.

### New UI

A redesigned page (title-block header, pucks and link health, a Map / 3D switch) sits next to the
current one in `web/ui-overhaul/index.html`, also one static file with no network assets. It uses the
same endpoints, so the server only needs to serve it at `/`:

```bash
python scripts/dashboard.py --auto-ports --page web/ui-overhaul/index.html
python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json --page web/ui-overhaul/index.html
python scripts/dashboard.py --simulate-room --page web/ui-overhaul/index.html   # no hardware
```

`--page` works with every source and with the usual `--http-port`, `--host` and `--room` options; open
<http://127.0.0.1:8000/> as before. The path is relative to the working directory, so run from the
repository root; a path that is not a file stops the server at start. Numbers this server does not send
yet (packet loss, clipping, gains, the estimator, the last motion time) show a dash, the 3D view shows the
room without a building plan, and the calibration form starts at the current page's values (3 clips of
10 s, noisy room).

To go back, start the dashboard without `--page`: it serves `web/index.html`, which is unchanged.
Deleting `web/ui-overhaul/` removes the new page.

### Motion track

The ringed dot on the map (`track` in the payload, `csi/tracker.py`) follows the heatmap's hot blob with a
Kalman filter, so it glides instead of jumping. It is the heat-weighted centre of the cells within 45% of the
hottest one, smoothed over time; it fades as its confidence (the blob's heat) drops, and disappears 3 s after
the map goes cold. Like the heatmap it shows where change is, not where a person is: it assumes one moving
person, and with four links it is drawn towards the link lines and where they cross.

On the simulator (`python scripts/track_eval.py`, the demo walk at 0.5 m/s, 37 Hz) the dot is a median 0.59 m
from the true position (p90 0.86 m), against 0.91 m for the hottest cell. No track appeared in a simulated
empty room. On the rig the heat peaks low (median about 0.2), so the filter's measurement noise is fixed
rather than scaled down with the heat: the dot trails a walking person by about 0.05 m and catches up with a
jump in about 0.5 s, at any heat. It appears once the hottest cell reaches 0.1.

The payload's `track` is `{x, y, vx, vy, confidence}` (metres, m/s, 0..1), or `null` when there is no track.

### Reading the heatmap

- Each link is judged against its own quiet baseline: the median of its own score over the last 60 s. A
  link needs 10 s of history before it contributes.
- Evidence is `log(score / baseline)` mapped to 0..1: ratios up to 1.2 are noise, and 3 times the
  baseline is full evidence.
- Only EMPTY and MOTION links contribute. Each link's evidence, times its weight (`link_weights` in
  `config/room.json`, default 1.0), is spread along its sender-receiver line with a Gaussian falloff of
  0.35 m. Where two links agree the cell is hotter (`agreement` 0.5).
- `sensitivity` (default 1.3) is the soft-saturation gain. Cells rise in 0.3 s and cool in 0.3 s
  (`rise_tau_s`, `decay_tau_s` in `status.parameters`), so the map follows people quickly. The grid is
  28 x 18 cells. The page's sliders start at these values, and `POST /api/params` changes them live.
- The threshold is shown but does not decide heat.
- The map shows change, not presence: a person who stands still for more than about 30 s is absorbed into
  the baseline and fades.

### Auto-threshold mode

`--auto-threshold` is **experimental**. It ignores any `--thresholds` file and applies to live sources
only (`--port`, `--auto-ports`, `--simulate-room`). It adapts to each link's noise, needs about 10 s of
quiet at the start, and absorbs a person who stays still for about 25 s. It detects walking less
sensitively, because the room needs two links to agree.

- Each link is judged against its own adaptive baseline: the median of its score over the last 75 s of
  packet time, with a robust spread (1.4826 x MAD).
- A link is MOTION (elevated) when its score stays at or above both `baseline + 5.5 x spread` and
  `2 x baseline` for 0.6 s, and leaves below a lower exit level. It is WARMUP for the first 10 s.
- If most of the last 30 s sits above the enter level, that level becomes the new normal (a receiver
  re-baseline, or a room that stays busy).
- The room is MOTION when at least two links are elevated together, or one link is well above its enter
  level for 0.6 s.
- A Set on a link makes it fixed; `"value": null` returns it to auto.
- The payload carries `mode`, `elevated` and `level` per link, and `status.room`.

### Dashboard options

Exactly one packet source is required:

| Source | Meaning |
|---|---|
| `--auto-ports` | Find each enabled receiver's port by its USB serial (`"serial"` in the room file). Fails, naming them, if any enabled receiver is not connected, has no serial, or matches more than one port. |
| `--port [NAME=]PATH` | A receiver's serial port; repeat for each receiver. A value is named when the part before the first `=` contains no `/`. Unnamed values become `rx1`, `rx2`, ... skipping names already given. A port named after a disabled node is never opened. |
| `--simulate-room` | A simulated person walks a loop through the room in real time. No hardware. |
| `--simulate` | A simulated transceiver rig: the room's transceivers send [round broadcasts](round-broadcast.md) while a person walks a loop, in real time. No hardware. Needs a room with at least two transceivers, for example `--room config/room_tr8.json` (8 nodes, 56 links). Every link is in auto mode. |
| `--replay run.jsonl` | Serve a run written with `--log`. See [Logging and replay](#logging-and-replay). |
| `--replay-session DIR` | Feed a recorded session through the live pipeline. See [Logging and replay](#logging-and-replay). |

Other options:

| Option | Default | Meaning |
|---|---|---|
| `--thresholds links.json` | none | Per-link fixed thresholds, from `links_live.py --calibrate`, **Save thresholds** or `calibrate.py --apply`. This is the default mode. Without it, live links show UNCALIBRATED until calibrated or set; `--simulate-room` calibrates on a simulated empty room, and `--replay-session` on the session's first empty recording. |
| `--auto-threshold` | off | Experimental auto mode; see [Auto-threshold mode](#auto-threshold-mode). |
| `--save-thresholds PATH` | the `--thresholds` file, else `links_thresholds.json` in the working directory | Where **Save thresholds** writes. |
| `--room PATH` | `config/room.json`, or `$CSI_ROOM` when set | Room configuration. |
| `--baud N` | 921600 | Serial baud rate. |
| `--seed N` | 0 | Simulator seed. |
| `--sim-quiet-first SECONDS` | 0 | With `--simulate-room` or `--simulate`: the simulated room stays empty this long before the walk starts, so Calibrate can be tried. 60 is a good value; a default calibration takes about 45 s. |
| `--log run.jsonl` | none | Append the state stream to a file. |
| `--speed 1\|N\|max` | 1 | Replay speed. |
| `--margin X` | 1.25 | With `--replay-session` and no `--thresholds`: the calibration margin. |
| `--http-port N` | 8000 | HTTP port. |
| `--host ADDR` | `127.0.0.1` | Address to listen on; `0.0.0.0` for the network. See [Security](#security). |
| `--allow-control` | off | Accept commands when `--host` is not a loopback address. See [Security](#security). |
| `--page PATH` | `web/index.html` | The page served at `/`. See [New UI](#new-ui). |

The server checks the combination at start and exits with an error when:

- more than one source, or none, is given;
- `--speed` is given without `--replay` or `--replay-session`;
- `--sim-quiet-first` is given without `--simulate-room` or `--simulate`;
- `--thresholds` or `--raw-log` is combined with `--simulate`;
- `--simulate` is given a room with fewer than two transceivers;
- `--margin` is given without `--replay-session`;
- `--log` or `--thresholds` is combined with `--replay` (a log already holds its states);
- `--auto-threshold` is combined with a replay;
- `--page` does not name a file.

### HTTP endpoints

| Method and path | What it does |
|---|---|
| `GET /` | The room page (also at `/index.html`). |
| `GET /api/state` | One JSON payload: room, nodes, links, heatmap and status. |
| `GET /api/events` | The same payload as Server-Sent Events, at most about 5 per second. The last event has `status.finished` true when the sources end, and the stream closes. |
| `GET /api/thresholds` | The current thresholds and the save path, as `{thresholds, path}`. Not served in replays. |
| `POST /api/calibrate` | Body `{clips, seconds, margin, lead_s, noisy}`, every field optional; defaults 3, 10, 1.25, 5 and `true` (noisy-room rules). Runs the countdown and clips and returns 202. 409 while one is running. |
| `POST /api/calibrate/cancel` | Stops a running calibration. 409 when none is running. |
| `POST /api/threshold` | Body `{link, value}` sets one link's threshold. `value` is a positive number, or `null` to return the link to auto in auto mode. |
| `POST /api/thresholds/save` | Writes all thresholds as `{label: number}` to the save path and returns `{path, thresholds}`. 409 when there is nothing to save. |
| `POST /api/params` | Body with any of `{sensitivity, decay, rise, falloff}` changes the heatmap live. Ranges: `sensitivity` 0.01 to 10, `decay` 0.05 to 60 s, `rise` 0.01 to 10 s, `falloff` 0.05 to 2 m. |

Status codes for the commands (the `POST` endpoints):

| Code | When |
|---|---|
| 400 | A bad value, an unknown field, or a body that is not a JSON object. |
| 403 | Commands are off (see [Security](#security)), or the `Host` or `Origin` header is not this server. |
| 409 | A replay is running (replays are read-only), a calibration is already running, or there is nothing to cancel or save. |
| 413 | The body is larger than 64 KiB. |
| 415 | `Content-Type` is not `application/json`. |

Any other path returns 404. The full payload schema and the server's internals are in
[architecture.md](architecture.md). To embed the server in Python, the entry point is
`csi.server.DashboardServer(room, sources, thresholds)`, where `sources` maps receiver names to
`(host_ts, line)` streams.

The `curl` examples in this guide use POSIX shell quoting; on Windows, run them from Git Bash or adjust
the quoting for `curl.exe`.

### Security

The server binds to `127.0.0.1` by default, so only this machine can reach it.

- `--host 0.0.0.0` listens on the network with **no authentication**. Anyone who can reach the port can
  read the state, and the commands are refused unless you also pass `--allow-control`. Use it only on a
  network you trust.
- The five command endpoints (`/api/calibrate`, `/api/calibrate/cancel`, `/api/threshold`,
  `/api/thresholds/save`, `/api/params`) are enabled only when `--host` is a loopback address
  (`localhost`, `127.0.0.0/8` or `::1`), or when you pass `--allow-control`. Otherwise they return 403.
  The page's controls and `scripts/calibrate.py` use the same endpoints.
- Every command must also pass these checks, which the server makes before it runs anything:
  1. `Content-Type` must be `application/json`, with optional parameters such as `charset=utf-8`.
     Anything else gets 415. This blocks a cross-site form, which cannot send that media type without a
     preflight, and the server never grants one.
  2. `Host` must name this server: `localhost`, `127.0.0.1` or `[::1]`, with the bound port. With
     `--allow-control` and a `--host` other than loopback, that `--host` value is accepted too (as an IP
     address only, when `--host` is `0.0.0.0` or `::`). Anything else gets 403, which blocks DNS
     rebinding.
  3. `Origin`, when the browser sends one, must be an `http://` origin of this server. Anything else gets
     403.
- Bodies are limited to 64 KiB (413 above that), and a body that is not a JSON object gets 400. Unknown
  fields are refused.
- GET requests are never refused by these checks, and replays are read-only.

### Terminal tools

| Script | Shows | Example |
|---|---|---|
| `scripts/links_live.py` | One row per link: state, score against threshold, packet rate, drops, RSSI. | `python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json` |
| `scripts/detect_live.py` | The debounced room state for one port, one line per change. | `python scripts/detect_live.py --port <PORT> --threshold-file calibration.json` |
| `scripts/telemetry.py` | One status line per update for one port: link health, score against threshold, state. | `python scripts/telemetry.py --port <PORT> --threshold-file calibration.json` |
| `scripts/live.py` | A matplotlib window: amplitude heatmap, motion-variance trace, threshold line. Up or `+` raises the threshold, Down or `-` lowers it. | `python scripts/live.py --port <PORT> --threshold-file calibration.json` |

- Only one reader can hold a port, so stop the dashboard and any other reader first.
- `links_live.py` is the terminal view of the rig: it scores each (receiver, sender) link separately and
  needs `--thresholds` (a `links.json`) on real ports. It takes several `--port` values but not
  `--auto-ports`.
- `detect_live.py`, `telemetry.py` and `live.py` read one port and score it as a single link. On a
  receiver that hears two senders they mix both signals; `detect_live.py` and `telemetry.py` print a
  warning when they see a second sender. They need `--threshold-file` (from `detect_live.py --calibrate`)
  or `--threshold <score>` on a real port.
- `live.py` without `--save-frame` opens a window and needs a display.

### Breathing

Breathing detection (STILL_PRESENT) is **experimental and off by default**. On the `hw5` session it read
43% of empty windows (1305 of 3065) as STILL_PRESENT, so it is not trusted. To turn it on, add
`--breathing`:

```bash
python scripts/detect_live.py --port <PORT> --threshold-file calibration.json --breathing
python scripts/telemetry.py --port <PORT> --threshold-file calibration.json --breathing   # also shows breaths/min
python scripts/evaluate.py --breathing data/hw5                                         # also score the still_* labels
```

Breathing needs a full 15 s window, so the output says "not enough data" until then. Without
`--breathing`, the evaluator marks the still labels "breathing disabled" instead of reporting a misleading
0%. The dashboard does not run breathing detection.

### Logging and replay

These need no hardware and no serial ports:

```bash
python scripts/dashboard.py --simulate-room --log run.jsonl     # header line, then one payload per line
python scripts/dashboard.py --replay run.jsonl --speed 8        # the logged states; --speed 1 is the original pace
python scripts/dashboard.py --replay-session data/hw5 --speed 8 # a recorded session through the live pipeline
```

- `--log` appends the state stream to a file: a header line (room, thresholds, start time), then one
  payload per line, the same as `/api/events`. It works with any live source and with `--replay-session`,
  but not with `--replay`.
- `--speed` takes `1`, a number `N` (N times faster), or `max`.
- `--replay-session` calibrates on the session's first empty recording; a session with no empty recording
  is refused unless you pass `--thresholds`. Use `--margin X` to change the margin, or
  `--thresholds links.json` to skip calibration. A single-receiver recording replays as one link, and
  `status.messages` says so.
- A replay sets `status.finished` true at the end and keeps the final state up until Ctrl+C. Replays are
  read-only: commands return 409.

## Recording and evaluating

### Recording a session

`scripts/record.py` records one receiver's port into a labelled session folder:

```bash
python scripts/record.py --port <PORT_RX1> --session room1 --label empty --countdown 5 --notes "rx1 top-right, tx0 bottom-right, room empty"
```

Keys while it runs: `e` empty, `w` walking, `s` still_standing, `l` still_sitting_or_lying, `q` stop. Each
label switch closes the file and starts a new one after the countdown (`--countdown`, 5 s by default).
Keep the room in the state each label names while it records. `--duration` stops after that many
seconds.

- `record.py` holds the port, so the dashboard cannot use that receiver while it records.
- The label keys need a POSIX terminal. Native Windows Python has no `termios` module, so record from
  macOS or Linux.
- Recording into an existing session folder appends to it: earlier files are kept and new notes are
  added to the old ones. Every file in a session must share one subcarrier layout; a run that produces a
  different layout is refused, and you record into a new folder instead.

### Session folders

Sessions go under `data/` (the default `--data-dir`), which is gitignored:

```
data/room1/
  session.json                      placement notes, channel, send rate, date, subcarrier layout, file list
  empty_20261107T155236Z.csv        one label per file; the name is the label and the UTC start time
  walking_20261107T155324Z.csv
```

Each CSV has the columns `host_ts, device_ts, rssi, channel, mac, amp_0 ... amp_N`: one row per packet,
with the sender's address in `mac` and one amplitude column per kept subcarrier. A receiver records every
sender it hears into the same file. Load a session with `csi.recording.load_session`.

### Rehearsing without hardware

```bash
python scripts/record.py --simulate --session demo --label empty --countdown 0 --duration 20
```

`--simulate` replays the simulator's cycle (20 s empty, 10 s motion, 30 s still, looping) whatever label
you pass, so a 20 s file is all empty. Use simulated sessions to check that the pipeline runs, not to
measure accuracy.

### Evaluating

```bash
python scripts/evaluate.py data/hw5
python scripts/evaluate.py data/room1 data/room2          # several sessions
python scripts/evaluate.py --margin 1.5 data/hw5          # compare margins
```

For each session, the first `empty` recording (by start time) calibrates the threshold, with the
quiet-room rules and `--margin`. Every other recording is replayed through the detector, and each
decision is compared with the label of its file; the output is a confusion matrix per label. Splits are by
recording, never by random window. Only motion is scored by default (EMPTY and MOTION); add `--breathing`
for the still labels. A session with one empty recording scores no empty windows, because that recording
did the calibrating, and a session with no empty recording cannot be evaluated.

When a session's recordings hold several senders (the `mac` column), each sender is treated as a link:
it is calibrated on its own packets from the first empty recording and detected on its own, and the room
reads MOTION when any link does.

### Recorded data

Real recordings are on the `data/two-board-sessions` branch, not on `main`: `hw3` and `hw5`, both
single-link (one C5 receiver, one S3 sender, channel 6). The branch's `data/README.md` has the details.
To copy `hw5` into your `data/` folder without switching branches:

```bash
git archive data/two-board-sessions data/hw5 | tar -x
python scripts/evaluate.py data/hw5
```

No recording of the four-link rig is in the repository yet. Never switch branches in the main checkout to
reach the data: `data/` is gitignored on the working branch and tracked on the data branch, so switching
deletes it.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No data from a receiver | Another program holds its port. | Close the other readers: the dashboard, `idf.py monitor`, `record.py`, `telemetry.py`. On macOS, `lsof /dev/cu.usbmodem*` lists the holders; on Linux, `lsof /dev/ttyACM* /dev/ttyUSB*`. On Windows the open fails with "Access is denied". |
| A receiver shows failed in the status bar | Wrong port, the board was unplugged, or another program holds the port. | Read `status.errors` for the message. Check the port with `python scripts/ports.py`, then restart the dashboard (and recalibrate). |
| `--auto-ports cannot find: ...` | A receiver is not connected, has no `serial` in the room file, or its serial matches several ports. | Run `python scripts/ports.py`. Plug the board in, add its `serial`, or pass `--port NAME=PATH` explicitly. |
| A receiver failed after an esptool probe | The probe reset the board, and its stream stopped. | Restart the dashboard and recalibrate. Don't probe ports while readers run. |
| Thresholds are wrong after a reset | A reset re-records the gain baseline. | Reset in place, then recalibrate. |
| A link fires MOTION with nobody there | A noisy or bursty link. | Check packet rate and drops on its card. Raise the margin, or set a manual threshold with **Set**. Re-aim the antenna. |
| `rx1<-01` has a low packet rate | The known weak link: about 10 packets/s at RSSI -57 in the last calibration. | Check its rate and drops before trusting its threshold; re-aim or move the boards. |
| A link shows UNCALIBRATED | No threshold: no `--thresholds` file, the file lacks that link, or calibration skipped it. | Read `status.messages` for the reason, then calibrate again. |
| Calibration says "input too short" | Under 8 s of clean data after movement was rejected (quiet-room rules). | Record the empty room again with nobody walking through, or use the noisy-room rules or `scripts/calibrate.py`. |
| Calibration says "link too sparse" or "input too lossy" | Under 15 packets/s, or over 60% loss. | Check the sender's power and placement, and the receiver's cable. |
| `calibrate.py` exits with status 2 | `--apply` refused: a link got no threshold. | Sample longer (`--seconds 60`), or pass `--skip-bad-links`. |
| `calibrate.py` exits with status 1 | The dashboard is not running at `--url`, or refused a command. | Start the dashboard, check `--url`, and run on the dashboard's machine. |
| A link shows NO_SIGNAL | No packet for 2 s. | Check the sender's power and channel, and the receiver's cable. |
| A link shows WARMUP | Its motion window is not full yet. | Wait a few seconds. |
| A link looks like one noisy sender, or an unexpected link appears | Two senders share a `SENDER_ID`, or a forgotten board with a `1a:00:00:00:00:xx` address is still on. | Give every sender a unique ID and switch off spare boards. |
| `detect_live.py` or `telemetry.py` warns about several sender MACs | The tool scores one port as one link and is mixing senders. | Use `links_live.py` or the dashboard. |
| Commands return 403 | `--host` is not a loopback address, or the request's `Host` or `Origin` is not this server. | Use `http://127.0.0.1:<port>` or `localhost` with the bound port, or `--allow-control` on a network you trust. |
| Commands return 415 | `Content-Type` is not `application/json`. | Send `Content-Type: application/json`. |
| Commands return 409 | Replay mode, a calibration is already running, or nothing to cancel or save. | Use live mode, wait for the calibration to finish, or calibrate before saving. |
| Packet rate drops after adding a sender or receiver | Collisions on the shared channel, or an S3 receiver. | Lower the senders' rate to about 30 Hz with three senders, and use C5 receivers. See [Scaling up](hardware.md#scaling-up). |
| `--page ... is not a file` | The path is relative to the working directory. | Run from the repository root. |
| `record.py` refuses a session with a different number of subcarriers | Every file in a session must share one layout. | Record into a new session folder. |
| Breathing says "not enough data" | Less than 15 s of data. | Wait, or leave breathing off. |
| `flash.sh` refuses or fails | A dashboard is running, the toolchain is not set up, or the port holds another chip. | See [Flashing with the flash script](hardware.md#flashing-with-the-flash-script). |

## Command reference

Every flag below is taken from the scripts' argument parsers. `scripts/flash.sh` is in
[hardware.md](hardware.md#flashing-with-the-flash-script); `scripts/dashboard.py` is in
[Dashboard options](#dashboard-options).

The single-port tools (`detect_live.py`, `telemetry.py`, `live.py`, `record.py`) share these source
options:

| Option | Default | Meaning |
|---|---|---|
| `--simulate` | off | Use the simulator (20 s empty, 10 s motion, 30 s still, looping) instead of a port. |
| `--port PATH` | none | The receiver's serial port. One port only. |
| `--baud N` | 921600 | Serial baud rate. |
| `--rate HZ` | 100 with `--simulate` | The sender's nominal rate. `detect_live.py` uses it for the simulator only; the others also use it for drop estimates. On a real port leave it out, so the rate is estimated from device timestamps. |
| `--seed N` | 0 | Simulator seed. |

`detect_live.py`, `telemetry.py` and `live.py` also take a threshold, required on a real port:

| Option | Meaning |
|---|---|
| `--threshold-file PATH` | Calibration JSON from `detect_live.py --calibrate`. |
| `--threshold X` | A threshold on the detector's score scale. Single-link hardware 2 m apart scored about 3 to 9 empty and 25 to 70 walking, so calibrate rather than guess. The simulator default is 1 (empty about 0.2, motion 3 to 9). |

### scripts/links_live.py

| Option | Default | Meaning |
|---|---|---|
| `--simulate-links` | off | Simulated receivers x senders of the room. |
| `--port [NAME=]PATH` | none | A receiver's serial port; repeat for each. Unnamed ports get `rx1`, `rx2`, ... A port named after a disabled node is ignored. |
| `--room room.json` | `config/room.json` | Room for `--simulate-links` and for ignoring disabled nodes. |
| `--baud N` | 921600 | Serial baud rate. |
| `--rate HZ` | simulator 100; estimated on real ports | Sender's nominal rate; with `--calibrate` on real ports, the loss estimate uses it. |
| `--seed N` | 0 | Simulator seed. |
| `--thresholds links.json` | none | Per-link thresholds; required on real ports. |
| `--calibrate SECONDS` | none | Record an empty room, write per-link thresholds to `--out`, then exit. |
| `--clips N` | 1 | With `--calibrate`: N clips of SECONDS each, 3 s apart, pooled. |
| `--margin X` | 1.25 | With `--calibrate`: threshold = X x p99. |
| `--out PATH` | `links.json` | Where `--calibrate` writes. |
| `--duration S` | until Ctrl+C | Stop after S seconds. |
| `--every S` | 0.5 | Seconds between redraws. |

### scripts/detect_live.py

Source and threshold options above, plus:

| Option | Default | Meaning |
|---|---|---|
| `--calibrate SECONDS` | none | Learn a threshold from an empty room and exit. |
| `--margin X` | 1.25 | With `--calibrate`: threshold = X x p99. |
| `--out PATH` | `calibration.json` | Where `--calibrate` writes. |
| `--breathing` | off | Opt in to breathing detection (STILL_PRESENT). |
| `--debounce S` | 0.5 | Seconds a state must persist before it is reported. |
| `--duration S` | until Ctrl+C | Stop after S seconds. |

### scripts/telemetry.py

Source and threshold options above, plus `--breathing` (off; also shows breaths per minute) and
`--every S` (0.5; seconds between screen updates).

### scripts/live.py

Source and threshold options above, plus `--duration S` (stop after S seconds) and `--save-frame PNG`
(headless: render one frame to a PNG after `--duration`, 6 s if not given, then exit).

### scripts/record.py

Source options above, plus:

| Option | Default | Meaning |
|---|---|---|
| `--session NAME` | required | Session folder name. |
| `--data-dir DIR` | `data` | Parent directory for sessions. |
| `--label LABEL` | `empty` | Initial label: `empty`, `walking`, `still_standing` or `still_sitting_or_lying`. |
| `--countdown S` | 5 | Seconds before each recording. |
| `--notes TEXT` | empty | Node placement notes for `session.json`. |
| `--duration S` | until `q` or Ctrl+C | Stop after S seconds. |

### scripts/evaluate.py

| Option | Default | Meaning |
|---|---|---|
| `SESSION ...` | required | One or more session folders written by `record.py`. |
| `--breathing` | off | Also make breathing predictions and score the `still_*` labels. |
| `--margin X` | 1.25 | Margin for the per-session threshold calibrated on the empty recording. |

### scripts/calibrate.py

| Option | Default | Meaning |
|---|---|---|
| `--url URL` | `http://127.0.0.1:8000` | Dashboard base URL. |
| `--seconds S` | 45 | How long to sample the live scores. |
| `--apply` | off | Set the thresholds on the dashboard and save them. |
| `--skip-bad-links` | off | With `--apply`: apply the links that got a threshold, leave the others unchanged. |

### scripts/ports.py

| Option | Default | Meaning |
|---|---|---|
| `--room PATH` | `config/room.json` | Room configuration whose nodes the boards are matched against. |
