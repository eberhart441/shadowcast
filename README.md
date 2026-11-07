# WiFi CSI motion sensing with ESP32 nodes

Team 12's CodeMetalForge hackathon demo: device-free motion sensing from WiFi
channel state information (CSI). Fixed ESP32 boards running Espressif's
[esp-csi](https://github.com/espressif/esp-csi) firmware exchange packets, the
receivers stream CSI over USB serial, and Python on the laptop turns that into an
EMPTY / MOTION state per link and a rough room heatmap on a live web dashboard.
It also runs with no hardware, on a built-in simulator.

Contents: [What this is](#what-this-is) · [Quick start without hardware](#quick-start-without-hardware) ·
[The hardware rig](#the-hardware-rig) · [Flashing the firmware](#flashing-the-firmware) ·
[Calibrating](#calibrating) · [Running live](#running-live) ·
[Recording and evaluating](#recording-and-evaluating) · [How it works](#how-it-works) ·
[Results and known limits](#results-and-known-limits) · [Troubleshooting](#troubleshooting) ·
[Roadmap](#roadmap) · [Contributing](#contributing)

## What this is

The boards sit at fixed points in a room. A sender transmits packets, a receiver
records the channel state of each one, and the amplitudes change when someone
moves between them. The scripts score each sender-receiver pair (a *link*) for
motion, and the dashboard draws a rough map of where the change is.

| Part | Status |
|---|---|
| Motion detection (EMPTY / MOTION) per link | **Works on real hardware.** Single-link recording hw5: walking 100% MOTION, empty room 94% EMPTY. |
| Calibration from an empty room | **Works**, from the terminal or the dashboard. |
| Live dashboard (room map, link cards, heatmap) | **Works** on the four-node rig and on the simulator. |
| Recording and offline evaluation | **Works.** |
| Breathing (STILL_PRESENT) | **Experimental, opt-in.** It false-fires on empty rooms (43% of empty windows on hw5), so it is off by default. |
| Heatmap | **Rough.** A motion-likelihood estimate from four links. It shows where change is, not where a person is, and cannot count or separate people. It is only meaningful for one moving person, at zone-level accuracy. A person who stands still for about 30 s fades into the baseline. |

## Quick start without hardware

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                   # so `import csi` works from scripts/ and tests/
python -m pytest                   # 282 tests, a few minutes, no hardware needed
```

Start the dashboard with a simulated person walking a loop through the room:

```bash
python scripts/dashboard.py --simulate-room
```

Open <http://127.0.0.1:8000/>. If port 8000 is taken, add `--http-port 8001` and
open that port. Ctrl+C stops the server.

The same simulated room as a terminal table, one row per link:

```bash
python scripts/links_live.py --simulate-links
```

Other simulated tools (each runs until Ctrl+C unless noted):

```bash
python scripts/detect_live.py --simulate --duration 65     # EMPTY / MOTION, stops after 65 s
python scripts/telemetry.py --simulate                     # one status line per update
python scripts/live.py --simulate --save-frame frame.png --duration 6   # one frame to PNG, then exits
```

`live.py` without `--save-frame` opens a matplotlib window and needs a display.

## The hardware rig

Four boards at the corners of a 2.77 m x 1.84 m room, positions from
[`config/room.json`](config/room.json). The receivers are on USB to the laptop;
the senders run from battery or USB.

```
   x = 0 m                                         x = 2.77 m
  +--------------------------------------------------------+  y = 0
  |                                                        |
  | rx2  ESP32-C5 receiver          rx1  ESP32-S3 receiver |
  |                                                        |
  |                                                        |
  |            2.77 m x 1.84 m room                        |
  |                                                        |
  |                                                        |
  | tx1  ESP32-C5 sender              tx0  ESP32-S3 sender |
  +--------------------------------------------------------+  y = 1.84
```

| Node | Role | Chip | Position (x, y), m | Notes |
|---|---|---|---|---|
| `rx2` | receiver | ESP32-C5 | (0, 0), top-left | USB to laptop |
| `rx1` | receiver | ESP32-S3 | (2.77, 0), top-right | USB to laptop |
| `tx0` | sender | ESP32-S3 | (2.77, 1.84), bottom-right | MAC `1a:00:00:00:00:00`, battery or USB |
| `tx1` | sender | ESP32-C5 | (0, 1.84), bottom-left | MAC `1a:00:00:00:00:01`, battery or USB |

Every receiver hears every sender, so there are four links, named
`receiver<-last octet of sender MAC`: `rx1<-00` (right wall), `rx2<-01` (left wall),
and the diagonals `rx2<-00` and `rx1<-01`. The diagonal `rx1<-01` has weight 0.3 in
`link_weights` because it false-alarms on this rig.

Placement tips:

- Mount each board fixed, at roughly chest height, with its antenna facing into the room.
- Keep metal out of the line between boards.
- The original ESP32 board was noisy and is retired. The S3 receiver is noisier than
  the C5, so prefer the C5 as a receiver where you have a choice.
- Reset the receivers after placing the boards, before calibrating (see [Calibrating](#calibrating)).

The first single-link recordings used one C5 receiver and one S3 sender about 2 m
apart. That setup is retired; the layout above is the current rig.

## Flashing the firmware

We build the esp-csi `get-started` examples as shipped, with a few edits. Verified
with **ESP-IDF v5.5.6**; the ESP32-C5 needs v5.5 or newer. Install ESP-IDF, export it
(`. $IDF_PATH/export.sh`), then:

```bash
git clone https://github.com/espressif/esp-csi.git
cd esp-csi/examples/get-started
```

Our edits to the stock examples:

1. **Channel 6, fixed.** The examples fix the channel in `app_main.c` through the
   hard-coded `CONFIG_LESS_INTERFERENCE_CHANNEL` define, so the menuconfig channel
   alone is not enough. Set the same channel on both boards and check that the
   `CSI_DATA` lines report it.
2. **One SENDER_ID per sender.** Each sender in `csi_send` gets a SENDER_ID byte
   (`00` for `tx0`, `01` for `tx1`), so its MAC is `1a:00:00:00:00:<id>`.
3. **Receiver filter.** `csi_recv` accepts any sender whose MAC starts with
   `1a:00:00:00:00` (a `memcmp` of the first 5 bytes).
4. **Gain locked.** `CONFIG_FORCE_GAIN=1` in `csi_recv`.
5. **40 Hz send rate.** `CONFIG_SEND_FREQUENCY` is 40 in `csi_send`. At 100 Hz, two
   senders collided and lost about 65% of packets.

Build each chip in its own build directory (`-B`), and run `set-target` there. We
once mixed up targets in a shared build directory; keep them apart.

Senders (`tx0` is an ESP32-S3, `tx1` an ESP32-C5; set SENDER_ID before each build):

```bash
cd csi_send
idf.py -B build-s3 set-target esp32s3
idf.py -B build-s3 -p <PORT> flash          # tx0
idf.py -B build-c5 set-target esp32c5
idf.py -B build-c5 -p <PORT> flash          # tx1
```

Receivers (`rx1` is an ESP32-S3, `rx2` an ESP32-C5):

```bash
cd ../csi_recv
idf.py -B build-s3 set-target esp32s3
idf.py -B build-s3 -p <PORT> flash          # rx1
idf.py -B build-c5 set-target esp32c5
idf.py -B build-c5 -p <PORT> flash          # rx2
idf.py -B build-c5 -p <PORT> monitor        # confirm CSI_DATA lines; exit with Ctrl+]
```

Notes:

- The CSI header columns differ per chip. The parser reads the header line that
  `csi_recv` prints at boot and infers the layout (including the C5's), so no
  per-chip setting is needed. If a line fails to parse, check the header first.
- The console baud rate is usually 921600 (`CONFIG_ESP_CONSOLE_UART_BAUDRATE`), and
  the scripts default to 921600 and take `--baud`. These numbers were written from
  memory and not checked against the esp-csi version we used; confirm them.
- Close `idf.py monitor` before running any Python script. Only one program can hold a port.

### Finding the serial port

| OS | Command | Typical name |
|---|---|---|
| macOS | `ls /dev/cu.*` | `/dev/cu.usbmodem101`, `/dev/cu.usbmodem2101` |
| Linux | `ls /dev/ttyUSB* /dev/ttyACM*` | `/dev/ttyUSB0` (may need `sudo usermod -aG dialout $USER`, then log in again) |
| Windows | Device Manager, Ports (COM & LPT) | `COM3` |

On macOS use the `cu.*` devices, never `tty.*`. Unplug and replug a receiver to see
which entry appears, and use the receiver's port, not the sender's. Or run
`python -m serial.tools.list_ports`.

Warnings:

- **Reading a chip ID with esptool resets the board.**
- **Never probe a receiver's port while the dashboard or any other reader uses it.**
- **Resetting a receiver re-records its gain baseline** (from the first 100 packets
  after boot) **and shifts its thresholds.** Calibrate after the last reset.

## Calibrating

Each link has its own signal scale, so the dashboard uses **fixed thresholds by default**: from a
`--thresholds` file, from the calibration below, or from the Set button. Without any of these, links show
`UNCALIBRATED`. `--simulate-room` calibrates itself on a simulated empty room. The experimental
`--auto-threshold` mode needs no calibration (see [Dashboard options](#dashboard-options)). The terminal tools
(`links_live.py`, `detect_live.py`) need thresholds: they refuse to start without one rather than guess.

**Reset the receivers in place after placing the boards, before calibrating.** A
receiver that booted before the boards were placed keeps a gain baseline from the old
set-up. Place the boards, reset each receiver without moving anything (its reset
button, or unplug and replug), then calibrate with the room empty.

Two ways to calibrate, both with nobody in the room:

```bash
# Terminal, all links: three 10 s clips, pooled
python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --calibrate 10 --clips 3 --out links.json

# Terminal, one link (single port)
python scripts/detect_live.py --port <PORT> --calibrate 60 --out calibration.json
```

Or in the dashboard: **Controls, Calibrate on an empty room, Start calibration**.
The defaults are 3 clips of 10 s, margin 1.25, a 5 s "Clear the room" countdown, and
**noisy room** checked: movement in the clips is not refused, the threshold is p97 x margin over
all windows, and the result carries a warning with how much looked like movement. Untick it for the
quiet-room rules (a busy clip is refused). The result becomes fixed thresholds for those links.

What calibration does:

- Splits each clip into 1 s chunks and drops chunks with movement in them (a median
  well above the quiet baseline, or bursty scores), so a passer-by does not raise the threshold.
- Needs at least 8 s of clean data per link. Otherwise that link gets no threshold, and the reason is printed.
- Sets the threshold to `margin x p99`, the 99th percentile of the kept empty-room
  motion scores times the margin (default 1.25).
- Refuses a link that delivers under 15 packets/s or loses more than 60% of its
  packets. `links_live.py` measures loss against the rig's 40 Hz rate unless `--rate` says otherwise.
- In the dashboard, if more than half of a link's clip was rejected, the link keeps
  its previous threshold and shows a warning.

### Calibration margin

The margin sets the factor in `threshold = margin x p99` (`--margin` on
`links_live.py`, `detect_live.py` and `evaluate.py`, and the Margin field in the
dashboard). Lower margins catch brief or weak motion but raise false alarms; higher
margins are quieter but may miss weak motion. Measured on the single-link hw5 session:

| Margin | Empty windows read as MOTION | Walking windows read as MOTION |
|---|---|---|
| 1.0 | 14% | 100% |
| 1.1 | 11% | 100% |
| **1.25 (default)** | **6%** | **100%** |
| 1.5 | 5% | 100% |

Compare margins on your own recordings, and raise it for a noisy room:

```bash
python scripts/evaluate.py --margin 1.0 data/hw5
python scripts/evaluate.py --margin 1.5 data/hw5
```

### Calibration tips

- Clear the room and keep still while the clips record. Stand outside the lines between boards.
- Several short clips beat one long clip: a passer-by then costs only one clip.
- A bursty link (heavy loss) may need a manual threshold: use the dashboard's **Set**
  button for that link, or edit its value in `links.json`.
- After calibrating, compare each threshold with that link's EMPTY score. A threshold
  far above the empty score will miss motion; one far below it will false-alarm.

## Running live

### Dashboard on hardware

```bash
python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json
```

Open <http://127.0.0.1:8000/>. Unnamed `--port` values are named `rx1`, `rx2`, and so on.
A real port started without `--thresholds` still runs, but its links show
`UNCALIBRATED`, and `status.messages` explains why.

What the page shows (`web/index.html`, one static file with no network assets):

- **Room.** The four nodes at their configured positions (receivers and senders drawn
  differently), the links coloured by state, and the heatmap. A caption says it is a
  rough motion-likelihood estimate, not a position track.
- **Links.** One card per link: state, score against threshold, packet rate, drops,
  RSSI, whether it contributes heat, and a 30 s score sparkline.
- **Status bar.** Receivers (connected, finished or failed), link counts by state,
  calibration state, packet time, and messages.
- **Controls.** The calibration panel (clips, seconds per clip, margin, start, cancel),
  a threshold field and **Set** per link with **Save thresholds**, and heatmap sliders
  for sensitivity, decay, rise and falloff.
- **Pause** freezes the display while the server keeps running. The page reconnects by
  itself after a server restart.

### Reading the heatmap

- Each link is judged against its own quiet baseline: the median of its own score over
  the last 60 s. A link needs 10 s of history before it contributes.
- Evidence is `log(score / baseline)` mapped to 0..1: ratios up to 1.2 are noise, and 3x the baseline is full evidence.
- Only EMPTY and MOTION links contribute. Each link's evidence, times its weight
  (`link_weights` in `config/room.json`, default 1.0), is spread along its
  sender-receiver line with a Gaussian falloff of 0.35 m. Where two links agree, the cell is hotter (`agreement` 0.5).
- `sensitivity` (default 1.3) is the soft-saturation gain. Cells rise in 0.3 s and cool
  in 0.3 s (`rise_tau_s`, `decay_tau_s` in `status.parameters`), so the map follows people quickly. The grid is 28 x 18.
  The threshold is shown but does not decide heat. The page's sliders start at these values.
- The map shows change, not presence: a person who stands still for more than about
  30 s is absorbed into the baseline and fades.

### Dashboard options

- `--port [NAME=]PATH`: a receiver's serial port; repeat for each receiver.
- `--thresholds links.json`: per-link fixed thresholds, from `links_live.py --calibrate` or the dashboard. This is
  the default mode. Without the file, live links show UNCALIBRATED until calibrated or set; `--simulate-room`
  uses a calibration on a simulated empty room instead.
- `--auto-threshold`: **experimental** auto mode, which ignores any `--thresholds` file. It adapts to each link's
  noise, needs about 10 s of quiet at the start, and absorbs a person who stays still for about 25 s. It detects
  walking less sensitively, because the room needs two links to agree.
  Each link is judged against its own adaptive baseline: the median of its score over the last 75 s of packet
  time, with a robust spread (1.4826 x MAD). A link is MOTION (elevated) when its score stays at or above
  `baseline + 5.5 x spread` and `2 x baseline` for 0.6 s, and leaves below a lower exit level. It is WARMUP for the
  first 10 s. If most of the last 30 s sits above the enter level, that level becomes the new normal (a receiver
  re-baseline, or a room that stays busy). The room is MOTION when at least two links are elevated together, or one
  link is well above its enter level for 0.6 s. A Set on a link makes it fixed (`value: null` returns it to auto).
  The payload carries `mode`, `elevated`, `level` per link and `status.room`.
- `--simulate-room`: a simulated person walks a loop through the room in real time.
- `--http-port` (default 8000), `--host` (default `127.0.0.1`), `--room` (default
  `config/room.json`), `--baud`, `--seed`.
- `--save-thresholds PATH`: where **Save thresholds** writes. Defaults to `--thresholds`,
  else `links_thresholds.json` in the working directory.
- `--log`, `--replay`, `--replay-session`, `--speed`, `--margin`: see [Logging and replay](#logging-and-replay).
- `--allow-control`: see [Security](#security).

Ctrl+C stops the server and closes every port.

### HTTP endpoints

| Method and path | What it does |
|---|---|
| `GET /` | The room page. |
| `GET /api/state` | One JSON payload: room, nodes, links, heatmap and status. |
| `GET /api/events` | The same payload as Server-Sent Events, at most about 5 per second. The last event has `status.finished` true when the sources end, and the stream closes. |
| `POST /api/calibrate` | Body `{clips, seconds, margin, lead_s}`; defaults 3, 10, 1.25, 5. Runs the countdown and clips above. 409 while one is running. |
| `POST /api/calibrate/cancel` | Stops a running calibration. |
| `POST /api/threshold` | Body `{link, value}` sets one link's threshold. `GET /api/thresholds` lists them. |
| `POST /api/thresholds/save` | Writes all thresholds as `{label: number}` to the save path and returns it. |
| `POST /api/params` | Body `{sensitivity, decay, rise, falloff}` changes the heatmap live (`decay` and `rise` in seconds, `falloff` in metres). Unknown keys or out-of-range values give 400. |

Commands take a JSON body. In replay modes they return 409.

### Security

The server binds to `127.0.0.1` by default, so only this machine can reach it.

- `--host 0.0.0.0` listens on the network with **no authentication**. Use it only on a network you trust.
- Commands (calibrate, threshold, save, params) work only when `--host` is a loopback
  address. Any other `--host` returns 403 for them unless you also pass `--allow-control`.

The library entry point is `csi.server.DashboardServer(room, sources, thresholds)`,
where `sources` maps receiver names to `(host_ts, line)` streams.

### Terminal tools

| Script | Shows | Example |
|---|---|---|
| `scripts/links_live.py` | One row per link: state, score against threshold, packet rate, drops, RSSI. | `python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json` |
| `scripts/detect_live.py` | The debounced room state, one line per change. | `python scripts/detect_live.py --port <PORT> --threshold-file calibration.json` |
| `scripts/telemetry.py` | One status line per update: link health, score against threshold, state. | `python scripts/telemetry.py --port <PORT> --threshold-file calibration.json` |
| `scripts/live.py` | Matplotlib window: amplitude heatmap, motion-variance trace, threshold line. Up or `+` raises the threshold, Down or `-` lowers it. | `python scripts/live.py --port <PORT> --threshold-file calibration.json` |

Only one reader can hold a port, so close the others first. Each script has `--help`.

### Breathing (experimental, opt-in)

Off by default. On the hw5 session it read 43% of empty windows (1305 of 3065) as
STILL_PRESENT, so it is not trusted yet. To turn it on, add `--breathing`:

```bash
python scripts/detect_live.py --port <PORT> --threshold-file calibration.json --breathing
python scripts/evaluate.py --breathing data/hw5      # also score the still_* labels
```

Breathing needs a full 15 s window, so the output says "not enough data" until then.
Without `--breathing`, the evaluator marks the still labels "breathing disabled"
instead of reporting a misleading 0%.

### Logging and replay

These need no hardware and no serial ports:

```bash
python scripts/dashboard.py --simulate-room --log run.jsonl     # header line, then one payload per line
python scripts/dashboard.py --replay run.jsonl --speed 8        # the logged states; --speed 1 is the original pace
python scripts/dashboard.py --replay-session data/hw5 --speed 8 # a recorded session through the live pipeline
```

- `--speed` takes `1`, a number `N` (N times faster), or `max`.
- `--replay-session` calibrates on the session's first empty recording. Use `--margin X`
  to change the margin, or `--thresholds links.json` to skip calibration. A single-receiver
  recording replays as one link, and `status.messages` says so.
- A replay sets `status.finished` true at the end and keeps the final state up until
  Ctrl+C. Replays are read-only.

## Recording and evaluating

`scripts/record.py` records one receiver's port into a labelled session folder:

```bash
python scripts/record.py --port <PORT_RX1> --session room1 --label empty --countdown 5 --notes "rx1 top-right, tx0 bottom-right, room empty"
```

Keys while it runs: `e` empty, `w` walking, `s` still_standing, `l` still_sitting_or_lying,
`q` stop. Each label switch closes the file and starts a new one after the countdown
(5 s by default). Keep the room in the state each label names while it records.

Sessions go under `data/` (the default `--data-dir`), which is gitignored:

```
data/room1/
  session.json                      placement notes, channel, send rate, date, subcarrier layout, file list
  empty_20261107T155236Z.csv        one label per file; the name is the label and the UTC start time
  walking_20261107T155324Z.csv
```

Each CSV has the columns `host_ts, device_ts, rssi, channel, amp_0 ... amp_N`, one
amplitude column per kept subcarrier. Load a session with `csi.recording.load_session`.

To rehearse the loop without hardware:

```bash
python scripts/record.py --simulate --session demo --label empty --countdown 0 --duration 20
```

`--simulate` replays the simulator's cycle (20 s empty, 10 s motion, 30 s still, looping)
whatever label you pass, so a 20 s file is all empty. Use simulated sessions to check
that the pipeline runs, not to measure accuracy.

### Evaluating

```bash
python scripts/evaluate.py data/hw5
python scripts/evaluate.py data/room1 data/room2          # several sessions
python scripts/evaluate.py --margin 1.5 data/hw5          # compare margins
```

For each session, the first `empty` recording (by start time) calibrates the threshold.
Every other recording is replayed through the detector, and each decision is compared
with the label of its file. Splits are by recording, never by random window. Only motion
is scored by default (EMPTY and MOTION); add `--breathing` for the still labels. A session
with one empty recording scores no empty windows, because that recording did the calibrating.

### Recorded data

Real recordings are on the `data/two-board-sessions` branch, not on `main`: `hw3` and
`hw5`, both single-link (one C5 receiver, one S3 sender, channel 6). The branch's
`data/README.md` has the details. To copy `hw5` into your `data/` folder:

```bash
git archive data/two-board-sessions data/hw5 | tar -x
python scripts/evaluate.py data/hw5
```

No four-link recording is in the repository yet.

## How it works

1. **Parsing** (`csi/parser.py`). Each `CSI_DATA` line becomes one amplitude vector
   (`hypot` of each int8 I/Q pair). Null subcarriers are dropped with a mask locked
   from the first valid packet. A malformed line is counted and skipped, never fatal.
2. **Sources** (`csi/multisource.py`). Each receiver is read on its own thread with its
   own parser. A receiver that fails is reported in `status`, and the others keep running.
3. **Device time and health** (`csi/clock.py`, `csi/health.py`). The 32-bit device
   counter is unwrapped into a timeline that keeps moving across the wrap and a reboot.
   Packet rate and drops are estimated from device-time gaps.
4. **Motion score** (`csi/detect.py`). Per subcarrier, slow components below about 1 Hz
   are removed by a cascade of high-pass stages, so chest movement is largely suppressed.
   The variance of the residual over a 1 s window is averaged across subcarriers. The state changes after a 0.5 s debounce.
5. **Per-link monitor** (`csi/links.py`). Each (receiver, sender MAC) pair has its own
   scorer, debouncer, health and threshold. States: EMPTY, MOTION, WARMUP (no full window
   yet), UNCALIBRATED (no threshold), NO_SIGNAL (no packet for 2 s).
6. **Heatmap** (`csi/heatmap.py`, geometry in `csi/room.py`), as described in [Reading the heatmap](#reading-the-heatmap).
7. **Server** (`csi/server.py`). A standard-library HTTP server for the page, the JSON
   payload, the SSE stream (about 5 Hz) and the commands.

The payload, from `/api/state` and each SSE event:

```
room       {width_m, depth_m}
nodes      [{name, role, chip, x, y}]          receivers, then senders
links      [{id, receiver, sender, state, score, threshold, normalised, baseline,
             evidence, pkt_rate, drops, rssi, age_s, weight, contributes}]
heatmap    {cols, rows, extents, cells, max, contributing}   cells: row-major 0..1, row 0 = y 0
status     {time_s, links_by_state, parameters, receivers, errors, finished,
            calibrated_links, uncalibrated_links, messages, calibration, commands}
```

### Modules

| Module | Purpose |
|---|---|
| `csi/parser.py` | Parses `CSI_DATA` lines into amplitude vectors; infers the column layout per chip. |
| `csi/common.py` | Shared CLI options (`--simulate`, `--port`, `--baud`, `--rate`, `--margin`) and source setup. |
| `csi/clock.py` | Unwraps the device counter; detects wraps and reboots. |
| `csi/health.py` | Packet rate and drop estimates per link. |
| `csi/detect.py` | Motion score, debounced detector, calibration (robust chunks, margin, p99), threshold files. |
| `csi/breathing.py` | Experimental breathing rate from 15 s windows (0.1 to 0.5 Hz, PCA, FFT peak). |
| `csi/links.py` | Per-link monitoring and multi-link calibration; `links.json`. |
| `csi/multisource.py` | Reads several receivers concurrently into one stream. |
| `csi/room.py` | Room and node geometry, link footprints, simulated person positions. |
| `csi/heatmap.py` | The motion-likelihood heatmap. |
| `csi/dashboard.py` | `DashboardState`: receiver packets in, the JSON payload out. |
| `csi/server.py` | HTTP server: page, `/api/state`, `/api/events`, commands. |
| `csi/live_calibration.py` | The dashboard's calibration run (countdown, clips, processing). |
| `csi/replay.py` | Serves a logged run or a recorded session through the dashboard. |
| `csi/recording.py` | Writes and loads labelled session folders. |
| `csi/evaluate.py` | Offline evaluation and per-label confusion matrices. |
| `csi/simulate.py` | Simulator: the empty / motion / still cycle as CSI text lines. |
| `csi/viewer.py` | Rolling buffers for the `live.py` viewer. |
| `scripts/dashboard.py` | Dashboard server, logging and replays. |
| `scripts/links_live.py` | Per-link terminal table; `--calibrate`. |
| `scripts/detect_live.py` | Room state for one port; `--calibrate`. |
| `scripts/telemetry.py` | One status line per update, for one port. |
| `scripts/live.py` | Matplotlib viewer: amplitude heatmap, variance trace, threshold keys. |
| `scripts/record.py` | Labelled recorder with a countdown and label keys. |
| `scripts/evaluate.py` | Offline evaluation over recorded sessions. |
| `web/index.html` | The dashboard page. |

## Results and known limits

Measured with the current code on the recordings in `data/two-board-sessions`:

| Session | Setup | Result |
|---|---|---|
| hw5 (single link) | C5 receiver, S3 sender, about 2 m, channel 6, gain locked | Walking: 100% MOTION (2621 windows). Empty validation recording: 94% EMPTY at margin 1.25, 95% at 1.5. |
| hw3 (single link) | Same boards, gain not yet locked | Walking: 80% MOTION. Only one empty recording, which calibrated the threshold, so no empty windows were scored. |
| Breathing on hw5 | `--breathing` | 43% of empty windows read STILL_PRESENT. Not usable. |
| Four links | The rig above | No recording in the repository. Issue #9 records earlier numbers from the older rig: S3-sender links at about 38 packets/s each, and two links from the original ESP32 sender at 6 to 17 packets/s. Results were mixed. |

The data branch's README reports 95% EMPTY for hw5. Re-running `evaluate.py` on the
current code gives 94% at the default margin and 95% at 1.5.

Known limits:

- **One moving person, zone-level.** Two people blur together, and nothing counts people.
- **Stillness fades.** A person who stays still for about 30 s is absorbed into the
  baseline, so the heatmap is not a presence sensor.
- **Breathing is unreliable.** It is off by default.
- **Weak links are noisy.** Low packet rates make scores noisy. Issue #9 flags the
  left-wall and long-diagonal links as the weakest.
- **Thresholds go stale.** A receiver reset shifts its baseline, so recalibrate after resets.
- **Calibration needs a quiet room.** Movement during a clip is rejected, and too
  little clean data means no threshold for that link.
- **Little data.** The repository has two single-link recordings and no four-link or
  labelled still-state recordings yet.
- **No authentication.** The dashboard is for a local demo.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No data from a receiver | Another program holds its port. | On macOS, `lsof /dev/cu.usbmodem*` lists the holders. Close them: the dashboard, `idf.py monitor`, `record.py`, `telemetry.py`, or another reader. |
| A receiver failed after an esptool probe | The probe reset the board, and its stream stopped. | Restart the dashboard. Don't probe ports while readers run. |
| Thresholds are wrong after a reset | A reset re-records the gain baseline. | Reset in place, then recalibrate. |
| A link fires MOTION with nobody there | Noisy or bursty link. | Check packet rate and drops on its card. Raise the margin, or set a manual threshold with **Set**. Re-aim the antenna. |
| A link shows UNCALIBRATED | No threshold: no `--thresholds` file, or calibration skipped it. | Read `status.messages` for the reason, then calibrate again. |
| Calibration says "input too short" | Under 8 s of clean data after movement was rejected. | Record the empty room again, with nobody walking through. |
| A link shows NO_SIGNAL | No packet for 2 s. | Check the sender's power and channel, and the receiver's cable. |
| A link shows WARMUP | Its motion window is not full yet. | Wait a few seconds. |
| Commands return 403 | `--host` is not a loopback address. | Use localhost, or `--allow-control` on a network you trust. |
| Commands return 409 | Replay mode, or a calibration is already running. | Use live mode, or wait for the calibration to finish. |
| Breathing says "not enough data" | Less than 15 s of data. | Wait, or leave breathing off. |

## Roadmap

Nothing below is started.

- **Wireless receivers.** Receivers forward CSI over UDP instead of USB, on a shared
  router channel. The dashboard's source interface already takes named line streams,
  so a UDP receiver can plug in without changing the dashboard.
- **Floor-plan support.** Rooms defined by a floor plan, not a rectangle.
- **More nodes.** More senders and receivers; the link list grows with each pair.
- **Position tracking.** A tracker that follows the heatmap over time. An idea, not yet specified.
- **Fingerprinting.** Learned signatures for room states, in place of thresholds.

## Contributing

- **Integration branch: `csi-sensing`.** Feature and fix branches merge into it. It is
  the source of PR #8, which merges it into `main`.
- **Side branches: use a git worktree**, one per branch, off `csi-sensing`, so your
  main checkout stays where it is:

  ```bash
  git worktree add ../Team-12-my-fix -b fix/my-fix csi-sensing
  ```

- **Tests: `python -m pytest`.** The suite (282 tests, a few minutes) runs entirely on
  the simulator. It lives in `tests/`, one file per area: parser, detectors, calibration,
  links, dashboard server and page, recording, evaluation, breathing, and the simulator.
- **Issues and specs** are on GitHub: `gh issue view <number>`. See
  [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) for the conventions and
  [`CLAUDE.md`](CLAUDE.md) for the repository's agent notes.
- **Keep data out of git.** `data/` is gitignored. Share recordings through the data branch or another channel.
