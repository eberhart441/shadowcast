# WiFi CSI motion sensing with ESP32 nodes

Team 12's CodeMetalForge hackathon demo: device-free motion sensing from WiFi
channel state information (CSI). Fixed ESP32 boards running Espressif's
[esp-csi](https://github.com/espressif/esp-csi) firmware exchange packets, the
receivers stream CSI over USB serial, and Python on the laptop turns that into an
EMPTY / MOTION state per link and a rough room heatmap on a live web dashboard.
It also runs with no hardware, on a built-in simulator.

Contents: [What this is](#what-this-is) · [Quick start without hardware](#quick-start-without-hardware) ·
[The hardware rig](#the-hardware-rig) · [Scaling up](#scaling-up) · [Flashing the firmware](#flashing-the-firmware) ·
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
| Motion detection (EMPTY / MOTION) per link | **Works on real hardware.** Single-link recording hw5: walking 100% MOTION, empty room 94% EMPTY (measured with the previous 1 s scorer; see [Results](#results-and-known-limits)). |
| Calibration from an empty room | **Works**, from the terminal or the dashboard, with the quiet-room rules by default. |
| Live dashboard (room map, link cards, heatmap) | **Works** on the current four-link rig (two receivers, two senders) and on the simulator. Each link card shows the link's health: the sender's real rate and loss from its packet counter, clipping, gains and noise floor. |
| Recording and offline evaluation | **Works**, including scoring a labelled dashboard run with `scripts/eval_log.py`. |
| Breathing (STILL_PRESENT) | **Experimental, opt-in.** It false-fires on empty rooms (43% of empty windows on hw5), so it is off by default. |
| Heatmap | **Rough.** A motion-likelihood estimate from the links, normalised by how many links cover each spot. It shows where change is, not where a person is, and cannot count or separate people. It is only meaningful for one moving person, at zone-level accuracy. A person who stands still for about 30 s fades into the baseline. |

### Intended use and limits

This senses movement in a room, and from that it can suggest that someone is likely present. It is not a
life-detection device. It does not reliably detect people who are completely still. A missed detection is the
costly failure in search-and-rescue style use, so treat its output as decision support, never as the only
check that a room is empty.

## Quick start without hardware

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                   # so `import csi` works from scripts/ and tests/
python -m pytest                   # about 480 tests, several minutes, no hardware needed
```

Start the dashboard with a simulated person walking a loop through the room:

```bash
python scripts/dashboard.py --simulate-room
```

Open <http://127.0.0.1:8000/>. If port 8000 is taken, add `--http-port 8001` and
open that port. Ctrl+C stops the server.

To try the Calibrate button on the simulator, keep the simulated room empty for the first 90 s (the
default calibration, a 5 s countdown and 3 clips of 20 s with pauses, takes about 71 s):

```bash
python scripts/dashboard.py --simulate-room --sim-quiet-first 90
```

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

Two receivers and two senders at the corners of a 2.77 m x 1.84 m room, positions from
[`config/room.json`](config/room.json). Both receivers are ESP32-C5 boards on USB to the laptop;
the senders run from battery or USB. The rig runs on channel 6. Senders on the current firmware transmit at
20 Hz (about 19.6 packets/s); the rig numbers below were measured at the old 40 Hz.

```
   x = 0 m                                         x = 2.77 m
  +--------------------------------------------------------+  y = 0
  |                                                        |
  | rx2  ESP32-C5 receiver          rx1  ESP32-C5 receiver |
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
| `rx1` | receiver | ESP32-C5 | (2.77, 0), top-right | USB to laptop |
| `tx0` | sender | ESP32-S3 | (2.77, 1.84), bottom-right | MAC `1a:0c:12:00:00:00`, battery or USB |
| `tx1` | sender | ESP32-C5 | (0, 1.84), bottom-left | MAC `1a:0c:12:00:00:01`, battery or USB |

The sender addresses use the team prefix `1a:0c:12:00:00` of the current firmware. Boards still on the
old firmware (`1a:00:00:00:00:xx`) are not heard: reflash every board together (see
[Flashing the firmware](#flashing-the-firmware)).

A third receiver, `rx3` (ESP32-C5, top-middle at (1.385, 0)), is defined in `config/room.json`
with `"enabled": false`. It is not part of the rig: it is ignored everywhere until you set it
to `true` (see [Scaling up](#scaling-up)). `config/room_c5_only.json` is an earlier variant
(`rx1` disabled, `rx3` 1.5 m beyond the top wall, the old `1a:00:00:00:00:xx` sender addresses)
and does not describe the current rig.

Every receiver hears every sender, so there are four links, named
`receiver<-last octet of sender MAC`: `rx1<-00` (right wall), `rx1<-01` (long diagonal),
`rx2<-00` (C5 diagonal) and `rx2<-01` (left wall). At 40 Hz each ran at 36 to 38 packets/s with about
5% loss, estimated from device-time gaps. At 20 Hz expect about 19.6 packets/s per link; each link card
now shows the rate the sender really sends and the loss counted from its packet counter.

`link_weights` in `config/room.json` currently holds one entry, `"rx1<-01": 0.3`, which
multiplies that link's heatmap evidence by 0.3. It was set when `rx1` was an ESP32-S3
receiver and its long-diagonal link false-alarmed. It has not been re-measured with `rx1`
as a C5, so it may now be stronger than it needs to be.

Placement tips:

- Mount each board fixed, at roughly chest height, with its antenna facing into the room.
- Keep metal out of the line between boards.
- Use ESP32-C5 receivers. In quiet-room captures, links on an S3 receiver read MOTION 35 to 65% of the
  time, against 4 to 16% for the C5 receiver. The original ESP32 was noisy both as receiver and as
  sender, and is retired.
- Reset the receivers after placing the boards, before calibrating, and not after it (see [Calibrating](#calibrating)).

The first single-link recordings used one C5 receiver and one S3 sender about 2 m
apart. That setup is retired; the layout above is the current rig.

## Scaling up

The rig is defined in data. Nodes live in `config/room.json` under `"nodes"`, and the links are
every enabled receiver paired with every enabled sender. Adding a board means a config entry, a
flash and a recalibration; no code changes.

How the code treats the layout:

- **Links.** One per (receiver, sender) pair: 3 receivers and 2 senders give 6 links, and 3 and 3
  give 9. Check the list before any hardware is plugged in:

  ```bash
  python scripts/links_live.py --simulate-links --room tests/fixtures/room_5node.json --duration 2
  ```

- **Disabled nodes.** `"enabled": false` removes a node completely: no links, nothing in the
  dashboard payload, and its serial port is never opened. Its port can even be missing, so a node
  can be defined before its board is plugged in.
- **Positions.** A node may sit outside the room rectangle. The map frames the room and every node,
  and a node outside the room gets a dashed leader and an "outside room" label. The heatmap covers
  the room only.
- **Link weights** are keyed by link label, for example `"rx1<-01": 0.3`.

### Packet budget per receiver

The limit on scaling is each receiver's serial output, not the radio. A receiver prints every packet
from every sender it hears as one text line (about 0.85 KB on a C5) inside its Wi-Fi callback, at
921600 baud. That caps a C5 receiver at about 100 to 116 packets/s summed over all its senders, and an
S3 receiver at about 65 to 79. Collisions between senders are negligible at these rates. Every receiver
hears every sender, so **plan for at most about 80 packets/s per receiver: the sum of the real rates of
all the senders.**

| Senders | `CSI_SEND_HZ` | Real packets/s per sender | Per receiver |
|---|---|---|---|
| up to 4 | 20 (default) | 19.6 | 78 |
| 3 | 25 | 24.4 | 73 |
| 2 | 40 (the old rate) | 38.5 (measured) | 77 |

- Real rates sit below the nominal ones because the sender's sleep adds 1 ms to every period ("100 Hz"
  measured 90.9 packets/s, "40 Hz" 38.46).
- Do not go below 20 Hz: calibration refuses a link under 15 packets/s, and at 19.6 packets/s a link
  can lose only about 23% before it gets there.
- For more than 4 senders, build the receivers with `CSI_PRINT_STRIDE=4` (every 4th subcarrier, lines
  about 3x shorter) rather than lowering the rate. The gain in budget is estimated from the line length
  and has not been measured on the rig yet.
- Check every link's **Sent/s (seq)** and **Loss (seq)** on its card after any change: they come from
  the sender's own packet counter, so a receiver over its budget shows up as loss on all its links.
- On the laptop, each receiver is read on its own thread, and `serial_stream` reads whatever is
  waiting on the port in one call instead of a byte at a time.

The flags and the per-chip details are in [`firmware/README.md`](firmware/README.md).

### Adding a sender

1. Build `csi_send` with the next unused `SENDER_ID` (`-DSENDER_ID=0x02` for a third sender, in its own
   build directory; see [`firmware/README.md`](firmware/README.md)), with the same `CSI_CHANNEL` and
   `CSI_SEND_HZ` as the other senders. Its MAC becomes `1a:0c:12:00:00:02`, which the receivers'
   prefix filter already accepts. Check the [packet budget](#packet-budget-per-receiver) first.
2. Power it from battery or USB.
3. Add a sender node to `config/room.json` with its `mac`, `chip`, `x` and `y`.
4. Calibrate again (see [Calibrating](#calibrating)).

### Adding a receiver

1. Flash `csi_recv` for an ESP32-C5 (see [Flashing the firmware](#flashing-the-firmware)).
2. Add a receiver node to `config/room.json`, or set `"enabled": true` on one that is already there
   (as with `rx3`).
3. Plug it into its own USB port, which is required for each receiver.
4. Name its port on every command with `--port rxN=<PORT>`, for example:

   ```bash
   python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --port rx3=<PORT_RX3> --thresholds links.json
   python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --port rx3=<PORT_RX3> --calibrate 20 --clips 3 --out links.json
   ```

5. Power every sender, reset the receivers in place, then calibrate with the room empty. Only then are
   thresholds for the new links meaningful, and only until a receiver resets again.

### Limits

- **About 80 packets/s per receiver.** See the [packet budget](#packet-budget-per-receiver). Every sender
  you add costs every receiver its packet rate.
- **One channel, shared by all boards** (6 by default, 11 as the prepared fallback for a crowded venue).
- **One USB port per receiver.** Each receiver needs its own serial port.
- **Receivers must be ESP32-C5.** On this rig, a third S3 receiver cut the other receiver's packet
  rate from about 35 to 22 to 26 packets/s. A third C5 receiver did not: all links stayed at 34 to 38
  packets/s.
- **Wireless receivers are not built.** See the [Roadmap](#roadmap).

### Placement hints

From one walk test on this rig: a C5 receiver on the top wall between the two top corners was the
strongest sensor, with walking scoring 25 to 31 against a quiet maximum of about 11 (scores from the
previous 1 s scorer). The right-side links from a corner receiver can respond weakly, so check each
link's score during a walk before trusting its threshold.

Two layout examples for tests and the simulator live in `tests/fixtures/`: `room_5node.json` (three
receivers, two senders, `rx3` on the top wall at (1.385, 0)) and `room_star.json` (`rx3` 1.5 m beyond
the top wall). Both still list `rx3` as an ESP32-S3, which predates the receiver-chip finding above,
so treat them as layouts, not as a parts list.

### Planning a layout

Once the code is right, where the boards stand decides what they can see. `scripts/plan_layout.py` checks a
layout before anything is mounted. It uses the standard library only, so it runs without the venv:

```bash
python scripts/plan_layout.py config/examples/three_room_row.json
python scripts/plan_layout.py config/room.json --mode transceiver     # every board sends and receives
python scripts/plan_layout.py config/examples/l_house.json --json > plan.json
```

It reads the room config, plus optional keys that only it uses (`csi/room.py` ignores them):

- `footprint`: the building or section being watched, as a polygon (default: the room rectangle).
- `zones`: named rectangles `[x0, y0, x1, y1]` or polygons.
- `walls`: `{"a": [x, y], "b": [x, y], "db": 10}`, or a `material` instead of `db`. Typical losses are drywall
  3-5 dB, brick 8-12, concrete block 10-15 and reinforced concrete 15-25 or more.
- `send_hz`: the senders' rate.

`config/examples/` has four layouts: the current rig, a 12 x 5 m row of three rooms seen from both long sides,
an 8 x 6 m open floor in four zones, and an L-shaped house with walls (a first attempt with typical mistakes).

Reading the report:

- **Links** shows each link's length, its predicted RSSI and its flags. The RSSI is -32 dBm at 2 m (the hw5
  measurement), less 20 log10(d / 2 m) and the loss of every wall the link crosses. The flags are:
  - `SAME_SIDE`: both boards are on one side, so the link runs along the wall and sees almost nothing inside.
  - `OUTSIDE`: more than 30% of the link lies outside the footprint, not counting 1.5 m at each end, for
    example across the notch of an L.
  - `WEAK`: below -70 dBm. Expect a noisy score; the link needs a 2-3 s window or a better antenna.
  - `SHORT`: under 1.5 m.
- **Receivers** shows each receiver's RSSI spread and serial load. A spread over 6 dB is too much for one
  forced gain, which clips the near link and quantises the far one: equalise the distances or try
  `CSI_GAIN_LOCK_STRONGEST`. The serial load is checked against the [packet budget](#packet-budget-per-receiver).
- **Coverage** is a map with one character per 0.25 m cell, north up. A digit is the number of links whose
  sensitive band covers that cell; the band reaches 0.6 m plus 5% of the link's length either side of its
  line. The digits `0` to `2` mark blind cells, since placing a person takes three crossing links. A `.` is
  outside the footprint and `R` or `S` marks a board. Each zone lists the links through it and how many
  different receivers and senders they come from.
- **Sides and accuracy** counts the sides with a board within 1.5 m outside: aim for at least 3, ideally 4.
  The accuracy is what the literature reached at this spacing along the perimeter: boards about 1 m apart
  gave sub-metre, 2 m gave 1-1.5 m and 3 m gave room level.
- **Suggestions** come most important first:
  - which board to move off a shared wall, and where to put it
  - where to add boards (up to three, each placed after the ones before it)
  - which receivers and links to fix

These are rules of thumb from the radio tomography literature and the team's measurements, not a
propagation model. Walk-test every link before trusting it.

## Flashing the firmware

The firmware is in [`firmware/`](firmware/): Espressif's esp-csi `get-started` examples (`csi_send`,
`csi_recv`) with our edits, built with **ESP-IDF v5.5.6** (the ESP32-C5 needs v5.5 or newer).
[`firmware/README.md`](firmware/README.md) has the setup, the build and flash commands, every build flag
with examples, the channel-11 fallback and the cautions before flashing a receiver. Read it before
touching a board.

What the firmware does by default:

1. **Team address prefix `1a:0c:12:00:00`.** A sender's MAC is the prefix plus its `SENDER_ID`
   (`1a:0c:12:00:00:00` for `tx0`, `:01` for `tx1`). Receivers set their own MAC to
   `1a:0c:12:00:00:fe` and accept any sender with the prefix, so stock esp-csi boards
   (`1a:00:00:00:00:xx`) are ignored.
2. **Channel 6**, HT40, the same on every board.
3. **20 Hz senders** (about 19.6 packets/s real), so up to 4 senders fit the
   [packet budget](#packet-budget-per-receiver) of a receiver.
4. **Forced receiver gain.** A receiver forces the median gain of the first 50 packets after boot (all
   senders mixed) at packet 100. The per-packet gain log line of the stock example is compiled out.

Build flags, passed on the `set-target` line of a fresh build directory
(`idf.py -B <dir> -D<FLAG>=<value> set-target <chip>`):

| Flag | Firmware | Default | Use |
|---|---|---|---|
| `SENDER_ID` | `csi_send` | `0x00` | Last byte of the sender's MAC, one per sender (`0xfe` is the receivers'). |
| `CSI_SEND_HZ` | `csi_send` | `20` | Send rate. Keep the sum of the senders' real rates under about 80 per receiver. |
| `CSI_CHANNEL` | both | `6` | Channel 5 to 11, the same on every board. 11 is the prepared fallback. |
| `CSI_PRINT_STRIDE` | `csi_recv` | `1` | Print every Nth subcarrier. `4` makes lines about 3x shorter, for more senders. |
| `CSI_FORCE_GAIN`, `CSI_PRINT_RAW` | `csi_recv` | `1`, `0` | AGC left on with raw values: for A/B recordings, not the demo. |
| `CSI_GAIN_LOCK_STRONGEST`, `CSI_GAIN_LOCK_OFFSET` | `csi_recv` | `0`, `0` | Experimental: lock the strongest sender's gain so its link does not clip. |

After changing `CSI_PRINT_STRIDE` or any gain flag, recalibrate. The usual builds:

```bash
. ~/esp/esp-idf/export.sh
cd firmware/csi_recv                                          # receivers: rx1, rx2 and any new one are C5
idf.py -B build_c5 set-target esp32c5 && idf.py -B build_c5 build
idf.py -B build_c5 -p <PORT> flash
cd ../csi_send                                                # senders: one build directory per SENDER_ID
idf.py -B build_s3_id00 -DSENDER_ID=0x00 set-target esp32s3 && idf.py -B build_s3_id00 build
idf.py -B build_s3_id00 -p <PORT> flash                       # tx0 (ESP32-S3)
idf.py -B build_c5_id01 -DSENDER_ID=0x01 set-target esp32c5 && idf.py -B build_c5_id01 build
idf.py -B build_c5_id01 -p <PORT> flash                       # tx1 (ESP32-C5)
```

Run them in this order: the build directories of one project share its `sdkconfig`, and each `set-target`
rewrites it, so flash each image straight after building it. To flash a pre-built image later, use the
`esptool` command in [`firmware/README.md`](firmware/README.md). Flash one port at a time.

**Reflash every board together.** Boards on the old address prefix and on the new one do not hear each
other, and `config/room.json` lists the new sender addresses. Flash every sender and every receiver from
the same commit in one session, check each sender's boot line (`send_frequency: 20, mac:
1a:0c:12:00:00:<id>`) and the addresses in the receivers' `CSI_DATA` lines, then recalibrate. While
boards are mixed, links go silent, and the dashboard can list a link twice: the expected one as
NO_SIGNAL, and an old-address one under the same label.

Notes:

- The CSI header columns differ per chip. The parser reads the header line that
  `csi_recv` prints at boot and infers the layout (including the C5's), so no
  per-chip setting is needed. If a line fails to parse, check the header first.
- The console runs at 921600 baud (`firmware/csi_recv/sdkconfig.defaults`), and the scripts default to
  921600 and take `--baud`.
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
- **Resetting a receiver re-rolls its forced gain** (the median of the first 50 packets after boot,
  forced at packet 100) **and shifts its scores 3 to 5 times.** Calibrate after the last reset, and do
  not reset a receiver after that.

## Calibrating

Each link has its own signal scale, so the dashboard uses **fixed thresholds by default**: from a
`--thresholds` file, from the calibration below, or from the Set button. Without any of these, links show
`UNCALIBRATED`. `--simulate-room` calibrates itself on a simulated empty room. `--auto-threshold` needs no
calibration (see [Auto threshold](#auto-threshold-no-calibration)). The terminal tools (`links_live.py`,
`detect_live.py`) need thresholds: they refuse to start without one rather than guess.

**Calibrate after the last reset, and never restart afterwards.** A receiver forces its gain from the
first packets after it boots, and every reset (flashing, replugging, `esptool`, the reset button) picks a
new gain and shifts that receiver's scores 3 to 5 times. So:

1. Place the boards and power every sender.
2. Reset each receiver in place, without moving anything (its reset button, or unplug and replug).
3. Calibrate with the room empty.
4. From then on, leave every receiver and the dashboard running. If a receiver resets anyway,
   recalibrate its links.

Opening a port now keeps DTR and RTS (the lines that reset an ESP32) released, so restarting the dashboard
should no longer reset the receivers. This is not yet confirmed on the rig: until it is, treat a dashboard
restart as a reset.

**Recalibrate after updating to this version.** The motion scorer changed (a 2 s window and an
about 1 Hz high-pass, see [How it works](#how-it-works)), so older thresholds are on another scale (1.5 to
4x lower) and links read MOTION in an empty room. The dashboard, `links_live.py` and `detect_live.py`
warn at start about a thresholds file made with another scorer, and about any file that does not record
its scorer, which includes every file from before.

Two ways to calibrate, both with nobody in the room:

```bash
# Terminal, all links: three 20 s clips, pooled (a minute of empty room)
python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --calibrate 20 --clips 3 --out links.json

# Terminal, one link (single port)
python scripts/detect_live.py --port <PORT> --calibrate 60 --out calibration.json
```

Or in the dashboard: **Controls, Calibrate on an empty room, Start calibration**. The defaults are
3 clips of 20 s, margin 1.25, a 5 s "Clear the room" countdown, and **noisy room** unticked. The result
becomes fixed thresholds for those links.

The quiet-room rules are the default everywhere. The terminal tools always use them; the dashboard uses
the noisy-room rules only when **noisy room** is ticked.

Quiet-room rules (default):

- Splits each clip into 1 s chunks and drops chunks with movement in them (a median
  well above the quiet baseline, or bursty scores), so a passer-by does not raise the threshold.
- Needs at least 8 s of clean data per link, and at least half of the clip time must be clean.
  Otherwise that link gets no threshold, and the reason is printed.
- Sets the threshold to `margin x p99`, the 99th percentile of the kept empty-room
  motion scores times the margin (default 1.25).
- In the dashboard, if more than half of a link's clip was rejected, the link keeps
  its previous threshold and shows a warning.

Noisy-room rules (opt-in: tick **noisy room**, or send `"noisy": true`):

- Movement is never a refusal. Every window counts, and the threshold is `margin x p97`
  over all windows (default margin 1.25).
- The result carries a warning with how many seconds looked like movement.
- Use it only when the room cannot be cleared. Movement in the clips lifts the threshold above weak
  motion: in a replay of the recorded data, 2 s of walking in a 30 s clip cut weak-motion detection from
  74% to 8%.

Both modes:

- Refuse a link that delivers under 15 packets/s or loses more than 60% of its packets.
  Loss is measured against the sender's rate: `links_live.py --rate` sets it. Left out,
  the rate is estimated from device timestamps, as the dashboard does. (The link cards measure loss
  from the sender's packet counter instead, which needs no rate.)

`links_live.py --calibrate` writes `links.json` with the thresholds plus a record of the calibration: the
motion scorer, each link's packet rate during calibration, the margin and the time. `links_live.py` warns
when a link's live packet rate is more than 30% away from its calibration rate. The dashboard's **Save
thresholds** writes the plain `{label: threshold}` object without the record, so the next start warns that
the file does not record its scorer; for a file saved after updating, that warning is harmless. Both
shapes load everywhere.

### Auto threshold (no calibration)

`python scripts/dashboard.py --port ... --auto-threshold` judges each link against its own noise level,
learned online, and ignores any `--thresholds` file:

- Each link is WARMUP for about the first 12 s. After that it reads MOTION when its score rises to about
  1.5 to 2x its quiet level.
- While a link is in MOTION its baseline is frozen, so someone who keeps moving stays detected, for up to
  5 minutes. A level that lasts longer becomes the new normal.
- A receiver reboot restarts the warm-up of that receiver's links, so a re-rolled gain is relearned
  instead of read as motion.
- The room is MOTION when two links agree, or one link is well above its own level.

It is not the default. A lasting rise in background noise also counts as a level change, and can hold a
link in MOTION for up to 5 minutes, and a burst of noise in an empty room reads as MOTION. Run it on the
empty area for about 10 minutes and check it stays EMPTY before relying on it. The details are under
[Dashboard options](#dashboard-options).

### Calibration margin

The margin sets the factor in `threshold = margin x p99` under the quiet-room rules, or `margin x p97`
in noisy-room mode (`--margin` on
`links_live.py`, `detect_live.py` and `evaluate.py`, and the Margin field in the
dashboard). Lower margins catch brief or weak motion but raise false alarms; higher
margins are quieter but may miss weak motion. Measured on the single-link hw5 session with the previous
1 s scorer (re-run the commands below to measure the current one):

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
- Power every sender before the receivers boot, so each receiver's gain is taken with all of them on the air.
- Record about a minute of empty room in total (60 to 120 s). Several clips beat one long clip: a
  passer-by then costs only one clip.
- A bursty link (heavy loss) may need a manual threshold: use the dashboard's **Set**
  button for that link, or edit its value in `links.json`.
- After calibrating, compare each threshold with that link's EMPTY score. A threshold
  far above the empty score will miss motion; one far below it will false-alarm.

### Setting thresholds from live data

When a calibration refuses a link, or a clip looks wrong, you can set a threshold from live scores
instead. This is the method that worked on this rig; the code does not do it for you.

1. With the room empty, sample about 40 s of each link's score. The `links` array of `/api/state` (or
   one `/api/events` event) carries each link's `id` and `score`.
2. For each link, let `m` be the median of its samples. Let `s` be the median of `(m - x)` over the
   samples `x` at or below `m`. Then:

   ```
   threshold = max(m + 6 x 1.4826 x s, 3 x m)
   ```

   The lower half of the scores gives the noise spread, and it ignores movement because movement
   only adds to the upper tail.
3. Apply each value with the dashboard's **Set** control on that link, or by POST:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/threshold -H "Content-Type: application/json" \
     -d '{"link": "rx1<-00", "value": 12.3}'
   ```

4. Click **Save thresholds**, or POST `/api/thresholds/save`. The file is written to the
   `--save-thresholds` path: by default the `--thresholds` file, else `links_thresholds.json` in the
   working directory.

A Set makes that link's threshold fixed. Saving writes every current threshold to one file, which
`--thresholds` reloads on the next start.

Thresholds do not survive restarts of the receivers. A receiver re-rolls its forced gain whenever it
resets, and that shifts its scores, so a threshold tuned before a reset is stale. Opening a port used to
reset the receiver too; the ports now open with DTR and RTS released, which should stop that, but it is not
yet confirmed on the rig, so:

- Set thresholds **after** the last restart of the dashboard and of every receiver, and do not restart
  the dashboard once you have tuned it.
- Never run `esptool` against a port that a dashboard is using.

## Running live

### Dashboard on hardware

```bash
python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json
```

Open <http://127.0.0.1:8000/>. Unnamed `--port` values are named `rx1`, `rx2`, and so on.
A real port started without `--thresholds` still runs, but its links show
`UNCALIBRATED`, and `status.messages` explains why.

What the page shows (`web/index.html`, one static file with no network assets):

- **Room.** The nodes at their configured positions (receivers and senders drawn
  differently; a node outside the room is drawn with a dashed leader), the links coloured by state, and
  the heatmap. A caption says it is a rough motion-likelihood estimate, not a position track. A node
  with movement right next to it gets a ring, an "activity near" label and a note under the map (see
  [Reading the heatmap](#reading-the-heatmap)).
- **Links.** One card per link: state, score against threshold, packet rate, drops,
  RSSI, weight, whether it contributes heat, a 30 s score sparkline, and the link's health:
  - **Sent/s (seq)** and **Loss (seq)**: the rate the sender really sends and the share of its packets
    that never arrived, both from the sender's own packet counter over the last 10 s (the loss to trust;
    **Drops** is estimated from device-time gaps). Loss above 10% is flagged.
  - **Clipped**: the share of raw CSI values at the int8 limit over the last 5 s, flagged above 5%: the
    receiver's forced gain is too high for that sender. It reads slightly low on a C5, because the
    unused subcarriers are counted too.
  - **Gain agc/fft** and **Noise floor**, as the receiver prints them (only C5 receivers print gains). A
    gain that jumps to a new value and stays there usually means the receiver reset: recalibrate its
    links. (Whether a forced gain shows here unchanged is still to be checked on the rig.)
  - **Last motion**: how long ago the link was last in MOTION.
- **Status bar.** Receivers (connected, finished or failed), link counts by state,
  calibration state, packet time, the room's last motion, and messages (including warnings about a
  thresholds file from another motion scorer).
- **Controls.** The calibration panel (clips, seconds per clip, margin, **noisy room**, start,
  cancel; see [Calibrating](#calibrating)), a threshold field and **Set** per link with **Save
  thresholds**, and heatmap sliders for sensitivity, decay, rise and falloff. The controls send
  commands, so they work only when the server is allowed to take them (see [Security](#security)).
- **Pause** freezes the display while the server keeps running. The page reconnects by
  itself after a server restart.

### Reading the heatmap

- Each link is judged against its own quiet baseline: the median of its own score over
  the last 60 s. A link needs 10 s of history before it contributes.
- The baseline learns only while the link is EMPTY. While it is in MOTION the baseline is frozen, so a
  person who keeps moving stays on the map, and the link reads quiet again as soon as the movement stops.
  A freeze lasts at most 90 s (`baseline_freeze_s`): a level held longer, such as a receiver that reset to
  a new gain, becomes the new normal.
- Evidence is `log(score / baseline)` mapped to 0..1: ratios up to 1.2 are noise, and 3x the baseline is full evidence.
- Only links that report (EMPTY or MOTION, with a baseline) count. Each link covers a Gaussian band of
  0.35 m along its sender-receiver line, times its weight (`link_weights` in `config/room.json`,
  default 1.0). A cell's value is the weighted average evidence of the links that cover it, not their
  sum, so the nodes, where all of a node's links meet, no longer collect heat from every link, and a quiet
  link that crosses a cell pulls it down. A silent or uncalibrated link neither lowers nor raises a cell.
  Cells that no link really covers stay blank (`min_coverage`), and `coverage_eps` keeps a cell at a link's
  edge from taking that link's full evidence.
- Where two links that share no node both rise, the cell is hotter (`agreement` 0.5), so a person crossing
  two links stands out and a lone noisy link stays dim. One link at full evidence shows about 0.38, a
  crossing about 0.6.
- **Near-node veto.** When most of a node's links (at least 3 that report) rise together while the other
  links stay quiet, the movement is right next to that node, such as an operator beside an outside board.
  The node is listed in `heatmap.busy_nodes` and ringed on the map, and its links count with a tenth of
  their weight, so they do not paint a fan of heat across the room. It cannot fire on the four-link rig,
  where each node has two links.
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
- `--auto-threshold`: auto mode, no calibration (see [Auto threshold](#auto-threshold-no-calibration)). It
  ignores any `--thresholds` file. Each link keeps the last 180 s of its own score (packet time) and takes
  its baseline and a robust spread (1.4826 x MAD) from the lowest 60% of it, which movement cannot reach.
  A link is MOTION (elevated) when its score stays at or above `max(baseline + 4 x spread, 1.5 x baseline)`
  for 0.6 s, and leaves below `max(baseline + 2.5 x spread, 1.25 x baseline)`. It is WARMUP for about the
  first 12 s. While a link is elevated its baseline is frozen, for at most 300 s; a level that lasts longer
  becomes the new normal, and so does a level that drops for most of 30 s. A receiver reboot (its device
  clock restarts) restarts the warm-up of its links. The room is MOTION when at least two links are elevated
  together, or one link is at least 2x its enter level for 0.6 s. A Set on a link makes it fixed
  (`value: null` returns it to auto). The payload carries `mode`, `elevated`, `level` per link and
  `status.room`. The constants are at the top of `csi/adaptive.py`.
- `--simulate-room`: a simulated person walks a loop through the room in real time.
- `--sim-quiet-first SECONDS`: with `--simulate-room`, the simulated room stays empty for that
  long before the walk starts, so Calibrate can be tried (90 is a good value: the default calibration
  takes about 71 s).
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
| `POST /api/calibrate` | Body `{clips, seconds, margin, lead_s, noisy}`; defaults 3, 20, 1.25, 5 and `noisy` false (quiet-room rules). Runs the countdown and clips above. 409 while one is running. |
| `POST /api/calibrate/cancel` | Stops a running calibration. |
| `POST /api/threshold` | Body `{link, value}` sets one link's threshold. `GET /api/thresholds` lists them. |
| `POST /api/thresholds/save` | Writes all thresholds as `{label: number}` to the save path and returns it. |
| `POST /api/params` | Body `{sensitivity, decay, rise, falloff}` changes the heatmap live (`decay` and `rise` in seconds, `falloff` in metres). Unknown keys or out-of-range values give 400. |

Commands take a JSON body and must send `Content-Type: application/json` (see [Security](#security)).
In replay modes they return 409. A command returns 400 for a bad value, 409 when a calibration is
already running (or there is nothing to save), and 403 when controls are off.

### Security

The server binds to `127.0.0.1` by default, so only this machine can reach it.

- `--host 0.0.0.0` listens on the network with **no authentication**. Anyone who can reach the port can
  read the state, and the commands are refused unless you also pass `--allow-control`. Use it only on a
  network you trust.
- The five command endpoints (`/api/calibrate`, `/api/calibrate/cancel`, `/api/threshold`,
  `/api/thresholds/save`, `/api/params`) are enabled only when `--host` is a loopback address
  (`localhost`, `127.0.0.0/8` or `::1`), or when you pass `--allow-control`. Otherwise they return 403.
  The page's controls use the same endpoints.
- Every command must also pass these checks, which the server makes before it runs anything:
  1. `Content-Type` must be `application/json`, with optional parameters such as `charset=utf-8`.
     Anything else gets 415. This blocks a cross-site form, which cannot send that media type without a
     preflight, and the server never grants one.
  2. `Host` must name this server: `localhost`, `127.0.0.1` or `[::1]`, with the bound port. With
     `--allow-control` and a `--host` other than loopback, that `--host` value is accepted too (as an IP
     address only, when `--host` is `0.0.0.0` or `::`). Anything else gets 403, which blocks DNS rebinding.
  3. `Origin`, when the browser sends one, must be an `http://` origin of this server. Anything else gets 403.
- Bodies are limited to 64 KiB, and a body that is not a JSON object gets 400. Unknown fields are refused.
- GET requests are never refused by these checks, and replays are read-only.

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
- `--log` writes the motion-scorer configuration into the header, and every payload with its link health,
  so a log can be scored later with [`scripts/eval_log.py`](#scoring-a-labelled-dashboard-run).
- `--replay-session` calibrates on the session's first empty recording. Use `--margin X`
  to change the margin, or `--thresholds links.json` to skip calibration. A single-receiver
  recording replays as one link, and `status.messages` says so.
- A session recorded before the address-prefix change replays its `1a:00:00:00:00:00` sender as the
  room's first sender. A second old-prefix sender (`1a:00:00:00:00:01`) keeps its old address, so its link
  shows twice under one label: once as NO_SIGNAL and once with the recorded data.
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
The report names the motion scorer it used; calibration and detection always use the same one.

### Scoring a labelled dashboard run

`scripts/eval_log.py` scores a whole-rig run: a dashboard `--log` file plus a CSV of what really happened.
It needs only the standard library.

```bash
python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --thresholds links.json --log run.jsonl
python scripts/eval_log.py --log run.jsonl --labels labels.csv                  # summary on stdout
python scripts/eval_log.py --log run.jsonl --labels labels.csv --out run.json   # also the full result as JSON
python scripts/eval_log.py --log night.jsonl --labels quiet.csv                 # quiet.csv: one row "0,,empty"
```

The labels CSV has the columns `start_s,end_s,label` and an optional zone (the header row is optional):

```
start_s,end_s,label,zone
0,60,empty,
60,90,walking,"0.5,0.4,1.5,1.2"
90,,empty,
```

- Times are seconds from the first frame of the log. With `--absolute` they are host times instead (Unix
  seconds, or ISO 8601 date-times, read as local time unless they name a zone). An empty `end_s` runs to
  the end of the log. Lines starting with `#` are comments.
- A zone is a rectangle `x0,y0,x1,y1` in metres, in room coordinates (origin top-left, y down).
- `walking` and `motion` are scored for detection (`--motion-labels`), `empty` for false alarms
  (`--empty-labels`); any other label is reported with its share of frames in MOTION.

For each link and for the room state the dashboard showed, it reports: motion episodes detected, the share
of motion frames in MOTION, the latency from a label's start to the first MOTION, and false alarms per hour
of empty time. Every rate comes with a Wilson 95% interval. A frame with no decision (WARMUP, UNCALIBRATED,
NO_SIGNAL) counts as a miss in motion labels and is left out of the false-alarm time. Frames are not
independent, so quote the episode counts rather than the frame shares. It also reports:

- **Heatmap localisation**, for motion labels with a zone: the share of frames whose hottest cell is inside
  the zone, and the median distance to it (`--min-heat`, default 0.05, decides when the map is cold).
- **A scaling curve**: room MOTION recomputed as "at least k of n links in MOTION" (`--k`, default 2, the
  dashboard's rule) for each number of links n, over every subset of n links, or over `--max-subsets`
  (default 50) drawn with `--seed` when there are more. It ignores the room's hold time and strong-link
  rule, so its all-links point can differ a little from the room row.
- **Link health**: packets/s, and where the log has them the sender's rate and loss, clipping, noise floor
  and gains, with a warning when a gain changed during the run (a receiver reset).

### Recorded data

Real recordings are on the `data/two-board-sessions` branch, not on `main`: `hw3` and
`hw5`, both single-link (one C5 receiver, one S3 sender, channel 6). The branch's
`data/README.md` has the details. To copy `hw5` into your `data/` folder:

```bash
git archive data/two-board-sessions data/hw5 | tar -x
python scripts/evaluate.py data/hw5
```

No recording of the four-link rig is in the repository yet.

## How it works

1. **Serial input and parsing** (`csi/parser.py`). `serial_stream` opens each port with DTR and RTS
   released (so opening it should not reset the board), reads whatever is waiting in one call, and stamps
   each chunk's lines with the time it arrived. Each `CSI_DATA` line becomes one amplitude vector
   (`hypot` of each int8 I/Q pair). Null subcarriers are dropped with a mask locked
   from the first valid packet (learned from the first 20 lines for a `CSI_PRINT_STRIDE` build). The packet
   also carries the sender's packet counter (`seq` on a C5, `id` on an S3), the receiver's gains and noise
   floor, and how many raw values sit at the int8 limit. A malformed line is counted and skipped, never fatal.
2. **Sources** (`csi/multisource.py`). Each receiver is read on its own thread with its
   own parser. A receiver that fails is reported in `status`, and the others keep running.
   `receiver_stats()` gives each receiver's parser counters (lines, malformed, and so on).
3. **Device time and health** (`csi/clock.py`, `csi/health.py`). The 32-bit device
   counter is unwrapped into a timeline that keeps moving across the wrap and a reboot.
   Packet rate and drops are estimated from device-time gaps. Loss and the real send rate are counted
   from the sender's packet counter over the last 10 s (`seq_loss`, `seq_rate`), which needs no assumed
   rate; a counter wrap is unwrapped and a sender reboot starts the window over. The clipped share is kept
   over the last 5 s.
4. **Motion score** (`csi/detect.py`). Per subcarrier, slow components below about 1 Hz
   (breathing, drift) are removed by a cascade of three high-pass stages (time constant 0.32 s each), so
   chest movement is largely suppressed, while more of the walking energy (mostly at 0.3 to 1 Hz) gets
   through than with the previous corner of about 2 Hz. Each stage
   subtracts the average of its running mean before and after the update, which keeps the score scale, and
   so a threshold, nearly the same at any packet rate. The variance of the residual over a 2 s window is
   averaged across subcarriers. The state leaves MOTION after a 0.5 s debounce. These parameters are one
   `ScorerConfig`, `LIVE_SCORER`, used by the live monitor, every calibration path, the evaluators and the
   replays, and recorded in every calibration (`LEGACY_SCORER` is the previous 1 s scorer).
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
links      [{id, receiver, sender, mac, state, mode, elevated, level, score, threshold, normalised,
             baseline, evidence, pkt_rate, drops, rssi, age_s, weight, contributes, since_motion_s,
             seq_rate, seq_loss, clip_pct, agc_gain, fft_gain, noise_floor}]
heatmap    {cols, rows, extents, cells, max, contributing, busy_nodes}   cells: row-major 0..1, row 0 = y 0
status     {time_s, links_by_state, parameters, room, receivers, errors, finished,
            calibrated_links, uncalibrated_links, messages, calibration, commands}
```

- `seq_rate` (packets/s the sender sends), `seq_loss` and `clip_pct` (shares, 0..1) are null while a link
  is silent or until it has 20 packets; `agc_gain`, `fft_gain` and `noise_floor` are the newest packet's
  (null when the receiver does not print them). Logs from before these keys still load and replay.
- `since_motion_s`: seconds since the link was last in MOTION (0 while it is, null if never);
  `status.room` is `{state, elevated, mode, since_motion_s}`.
- `busy_nodes`: the nodes the near-node veto holds now; `status.parameters` lists the heatmap settings,
  including `coverage_eps`, `min_coverage`, `baseline_freeze_s`, `busy_evidence` and `busy_link_weight`.

### Modules

| Module | Purpose |
|---|---|
| `csi/parser.py` | Parses `CSI_DATA` lines into amplitude vectors and health fields; infers the column layout per chip; `serial_stream` reads a port in chunks. |
| `csi/common.py` | Shared CLI options (`--simulate`, `--port`, `--baud`, `--rate`, `--margin`) and source setup. |
| `csi/clock.py` | Unwraps the device counter; detects wraps and reboots. |
| `csi/health.py` | Per link: packet rate and drops, loss and real send rate from the sender's counter, clipping, gains. |
| `csi/detect.py` | Motion score and its one `ScorerConfig` (`LIVE_SCORER`), debounced detector, calibration (robust chunks, margin, p99 or noisy-room p97), threshold files. |
| `csi/adaptive.py` | `--auto-threshold` detector: per-link online baseline, frozen during motion, and a room decision from link agreement. |
| `csi/breathing.py` | Experimental breathing rate from 15 s windows (0.1 to 0.5 Hz, PCA, FFT peak). |
| `csi/links.py` | Per-link monitoring and multi-link calibration; `links.json` with its calibration record. |
| `csi/multisource.py` | Reads several receivers concurrently into one stream. |
| `csi/room.py` | Room and node geometry, links (every enabled receiver with every enabled sender), disabled nodes, simulated person positions. |
| `csi/heatmap.py` | The motion-likelihood heatmap: coverage-normalised, quiet-only baselines, near-node veto. |
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
| `scripts/eval_log.py` | Scores a labelled dashboard `--log` run: detection, latency, false alarms, localisation, scaling curve, link health. |
| `scripts/plan_layout.py` | Checks a board layout before mounting: link flags, predicted RSSI, receiver gain spread and serial load, coverage map, sides, accuracy and ranked suggestions. Standard library only. |
| `web/index.html` | The dashboard page. |
| `firmware/` | The ESP32 firmware (`csi_send`, `csi_recv`) and its build flags; see `firmware/README.md`. |

## Results and known limits

Measured with the previous motion scorer (1 s window, high-pass corner about 2 Hz) on the recordings in
`data/two-board-sessions`, and on the live rig where noted. Re-run `scripts/evaluate.py` to refresh them
for the current scorer:

| Session | Setup | Result |
|---|---|---|
| hw5 (single link) | C5 receiver, S3 sender, about 2 m, channel 6, gain locked | Walking: 100% MOTION (2621 windows). Empty validation recording: 94% EMPTY at margin 1.25, 95% at 1.5. |
| hw3 (single link) | Same boards, gain not yet locked | Walking: 80% MOTION. Only one empty recording, which calibrated the threshold, so no empty windows were scored. |
| Breathing on hw5 | `--breathing` | 43% of empty windows read STILL_PRESENT. Not usable. |
| Final four-link rig (rx1, rx2 C5; tx0 S3, tx1 C5), channel 6, 40 Hz | Measured on the rig | All four links at 36 to 38 packets/s, about 5% loss. Quiet-room median scores about 2 to 7, thresholds about 8 to 20. A continuous walk through the room reliably lights the left-wall and C5-diagonal links and the heatmap; the two rx1 links respond more weakly to walking in the tests. No labelled recording of this rig is committed, so there are no accuracy figures for it. |

The current scorer (2 s window, corner about 1 Hz) was compared with the previous one offline, on hw3 and
hw5 thinned to 25 packets/s and every 4th subcarrier, using a standard-library copy of the scorer rather
than `evaluate.py`. Walking detection on the weak hw3 link went from about 80% to 95 to 100%, with no false
alarms on held-out empty data (quiet-room rules, margin 1.25); the strong hw5 link's walking median grew
from 4.6x to 8.5x its empty-room p99; and thresholds calibrated at 25, 40 and 90 packets/s agreed within
about 5%. These are bench numbers, not a rig result.

Before the final rig, issue #9 recorded numbers from an older layout: S3-sender links at about 38 packets/s
each, and two links from the original ESP32 sender at 6 to 17 packets/s. Results were mixed.

The data branch's README reports 95% EMPTY for hw5. Re-running `evaluate.py` with the
previous scorer gave 94% at the default margin and 95% at 1.5.

Known limits:

- **One moving person, zone-level.** Two people blur together, and nothing counts people.
- **Stillness fades.** A person who stays still for about 30 s is absorbed into the
  baseline, so the heatmap is not a presence sensor.
- **Breathing is unreliable.** It is off by default.
- **Weak links are noisy.** Low packet rates make scores noisy. On the older layout, issue #9 flagged the
  left-wall and long-diagonal links as the weakest.
- **Thresholds go stale.** A receiver reset re-rolls its gain and shifts its scores 3 to 5 times, so
  recalibrate after resets. Thresholds from the previous motion scorer do not fit the current one.
- **Calibration needs a quiet room.** Movement during a clip is rejected by the quiet-room rules, and too
  little clean data means no threshold for that link. The opt-in noisy-room rules accept the clip and
  report how much of it looked like movement, at the cost of missing weak motion.
- **About 80 packets/s per receiver.** Each receiver's serial output caps the sum of its senders' rates.
  See the [packet budget](#packet-budget-per-receiver).
- **Little data.** The repository has two single-link recordings and no labelled recording of the
  four-link rig, or of still states on it.
- **No authentication.** The dashboard is for a local demo.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No data from a receiver | Another program holds its port. | On macOS, `lsof /dev/cu.usbmodem*` lists the holders. Close them: the dashboard, `idf.py monitor`, `record.py`, `telemetry.py`, or another reader. |
| A receiver failed after an esptool probe | The probe reset the board, and its stream stopped. | Restart the dashboard. Don't probe ports while readers run. |
| Thresholds are wrong after a reset | A reset re-rolls the receiver's forced gain. | Reset in place, then recalibrate. |
| Every link reads MOTION in an empty room after updating | The thresholds file is from the previous motion scorer (a warning says so at start). | Recalibrate (see [Calibrating](#calibrating)). |
| A link fires MOTION with nobody there | Noisy or bursty link. | Check packet rate, **Loss (seq)** and **Clipped** on its card. Raise the margin, or set a manual threshold with **Set**. Re-aim the antenna. |
| Links silent, or one listed twice, after flashing | Boards on the old and the new address prefix. | Reflash every board from the same commit (see [Flashing the firmware](#flashing-the-firmware)). |
| **Loss (seq)** high on every link of one receiver | The receiver is over its packet budget. | Lower the senders' rate or build the receivers with `CSI_PRINT_STRIDE=4` (see [Scaling up](#scaling-up)). |
| **Clipped** high on one link | The receiver's gain, forced from all senders, is too high for that strong sender. | Move that sender farther away or turn it, or try the experimental gain lock (`firmware/README.md`). |
| A link shows UNCALIBRATED | No threshold: no `--thresholds` file, or calibration skipped it. | Read `status.messages` for the reason, then calibrate again. |
| Calibration says "input too short" | Under 8 s of clean data after movement was rejected (quiet-room rules). | Record the empty room again, with nobody walking through. |
| A link shows NO_SIGNAL | No packet for 2 s. | Check the sender's power and channel, and the receiver's cable. |
| A link shows WARMUP | Its 2 s motion window is not full yet, or (auto mode) it is still learning its level. | Wait a few seconds (about 12 s in auto mode, again after a receiver reboot). |
| Commands return 403 | `--host` is not a loopback address, or the request's `Host` or `Origin` is not this server. | Use `http://127.0.0.1:<port>` or `localhost` with the bound port, or `--allow-control` on a network you trust. |
| Commands return 415 | `Content-Type` is not exactly `application/json`. | Send `Content-Type: application/json`. |
| Packet rate drops after adding a sender or receiver | The receivers' packet budget, or an S3 receiver. | Keep the senders' real rates under about 80 packets/s per receiver, and use C5 receivers. See [Scaling up](#scaling-up). |
| Commands return 409 | Replay mode, or a calibration is already running. | Use live mode, or wait for the calibration to finish. |
| Breathing says "not enough data" | Less than 15 s of data. | Wait, or leave breathing off. |

## Roadmap

Nothing below is started.

- **Wireless receivers.** Receivers forward CSI over UDP instead of USB, on a shared
  router channel. The dashboard's source interface already takes named line streams,
  so a UDP receiver can plug in without changing the dashboard.
- **Floor-plan support.** Rooms defined by a floor plan, not a rectangle.
- **More nodes.** The config already takes more senders and receivers (see [Scaling up](#scaling-up)).
  In practice each receiver's serial output (about 80 packets/s) is the limit.
- **Position tracking.** A tracker that follows the heatmap over time. An idea, not yet specified.
- **Fingerprinting.** Learned signatures for room states, in place of thresholds.
- **Speed up the test suite.** Marker tiers and parallel runs; the full suite takes several minutes.
- **Adaptive detection as the default.** `--auto-threshold` becomes the default once it has run clean on
  the empty rig and its false alarms from background-noise bursts are measured.
- **Accuracy work.** A labelled dataset with all links, filtering, and a small classifier compared with
  the threshold detector on held-out sessions.
- **Detecting still people.** Breathing is unreliable today, so a person who is completely still is not
  reliably detected.

## Contributing

- **Integration branch: `csi-sensing`.** Feature and fix branches merge into it. It is
  the source of PR #8, which merges it into `main`.
- **Side branches: use a git worktree**, one per branch, off `csi-sensing`, so your
  main checkout stays where it is:

  ```bash
  git worktree add ../Team-12-my-fix -b fix/my-fix csi-sensing
  ```

- **Tests: `python -m pytest`.** The suite (about 480 tests, several minutes) runs entirely on
  the simulator. It lives in `tests/`, one file per area: parser, detectors, calibration,
  links, dashboard server and page, recording, evaluation, breathing, and the simulator.
  `tests/conftest.py` points the room at `tests/fixtures/room_rig4.json` through `CSI_ROOM`, so
  reconfiguring the real rig in `config/room.json` does not change test outcomes. Many tests simulate
  long runs or start subprocesses, and two subprocess tests in `tests/test_dashboard_cli.py` can time
  out on a busy machine. Run one file with `python -m pytest -q tests/<file>`.
- **Issues and specs** are on GitHub: `gh issue view <number>`. See
  [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) for the conventions and
  [`CLAUDE.md`](CLAUDE.md) for the repository's agent notes.
- **Hardware and live-rig notes** (resets, ports, flashing, thresholds) are in
  [`docs/agents/hardware.md`](docs/agents/hardware.md). Read it before touching a board.
- **Never switch branches in the main checkout.** Work in a worktree instead. `data/` is gitignored on
  the integration branch but tracked on the data branch `data/two-board-sessions`, which holds
  `data/hw3`, `data/hw5` and `data/README.md`. Switching branches in the main checkout has deleted `data/`
  once.
- **Keep data out of git.** `data/` is gitignored. Share recordings through the data branch or another channel.
