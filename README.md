# WiFi CSI presence / motion / breathing detection

Device-free sensing with two ESP32 nodes running Espressif's
[esp-csi](https://github.com/espressif/esp-csi) firmware. One node sends
packets (`csi_send`), the other receives them and prints CSI over serial
(`csi_recv`). Everything runs on the laptop in Python.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                   # makes `import csi` work from scripts/
pytest                             # sanity check
```

Every script accepts `--simulate` to run with fake data and no hardware.

## Flashing the esp-csi examples (by hand)

Prereq: [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/)
installed and exported (`. $IDF_PATH/export.sh`). The receiver was verified
with **ESP-IDF v5.5.6** (ESP32-C5 needs v5.5 or newer).

```bash
git clone https://github.com/espressif/esp-csi.git
cd esp-csi/examples/get-started
```

Both nodes must use the **same WiFi channel**. Pick one (e.g. 6) and keep it
fixed. Use the router channel only if you are connecting through a router;
the simplest setup is ESP-NOW with no router.

**Channel is hard-coded in the examples.** The get-started examples fix the
channel in `app_main.c` via `CONFIG_LESS_INTERFERENCE_CHANNEL`, so do not rely
on the menuconfig channel alone. Set the same channel in `app_main.c` on both
boards, then check that the `CSI_DATA` lines report that channel.

### Sender node (`csi_send`, ESP32-S3)

```bash
cd csi_send
idf.py set-target esp32s3
idf.py menuconfig                # Example Configuration -> channel, send frequency
idf.py -p <PORT> flash
```

The sender transmits at 100 Hz by default (`CONFIG_SEND_FREQUENCY`; confirm in
menuconfig, and in `app_main.c` if your esp-csi version hardcodes it).

### Receiver node (`csi_recv`, ESP32-C5)

```bash
cd ../csi_recv
idf.py set-target esp32c5
idf.py menuconfig                # same channel as the sender; CONFIG_FORCE_GAIN=1
idf.py -p <PORT> flash
idf.py -p <PORT> monitor         # confirm lines starting with CSI_DATA
```

`CONFIG_FORCE_GAIN=1` is enabled in `csi_recv` to lock the gain.

(Close the monitor before running the Python scripts; only one program can
hold the port. Exit monitor with `Ctrl+]`.)

Notes:
- The CSI header columns differ per chip (ESP32-C5 vs ESP32-S3 vs ESP32). The
  parser reads the header line and infers the column layout, so no per-chip
  setting is needed. If a line fails to parse, check the header first.
- Some `csi_recv` versions use ESP-NOW; the receiver then filters by the
  sender's MAC (see `app_main.c`). Check the MAC matches your sender.
- The receiver's baud rate is usually 921600 (check `CONFIG_ESP_CONSOLE_UART_BAUDRATE`);
  the scripts default to 921600 and take `--baud`.
- Place nodes fixed, ~2-4 m apart, at roughly chest height, with the subject
  between or near the line of sight.

## Finding the serial port

| OS | Command | Typical name |
|----|---------|--------------|
| macOS | `ls /dev/cu.*` | `/dev/cu.usbserial-0001`, `/dev/cu.SLAB_USBtoUART` |
| Linux | `ls /dev/ttyUSB* /dev/ttyACM*` | `/dev/ttyUSB0` (may need `sudo usermod -aG dialout $USER`, then re-login) |
| Windows | Device Manager -> Ports (COM & LPT), or `mode` | `COM3` |

Or: `python -m serial.tools.list_ports`. Unplug/replug the receiver to see
which entry appears. Use the receiver's port, not the sender's.

## Calibrate, then run

Detection is motion-only by default: the states are `EMPTY` and `MOTION`.
Motion scores on real hardware are on a different scale from the simulator:
an empty room scores about 3-9 and walking about 25-70. So a real port always
needs an explicit threshold, either `--threshold-file` (from calibration) or
`--threshold <score>`. Without one the scripts exit with an error instead of
guessing.

**Calibrate in one command** (empty room, keep people out for the duration):

```bash
scripts/detect_live.py --port <PORT> --calibrate 60 --out calibration.json
```

It writes `calibration.json` with the threshold: `margin x p99`, the 99th percentile
of the empty-room motion score times the margin (`--margin`, default 1.25, see
[Calibration margin](#calibration-margin)). Calibration splits the recording into
1 s chunks and drops any chunk where someone was moving (its median is well above
the quiet baseline, or its scores are bursty), so a passer-by during the clip
does not inflate the threshold. It needs at least 8 s of clean data and reports
how many seconds were rejected; if it refuses, record the empty room again.

### Calibration margin

The threshold is `margin x p99`, where p99 is the 99th percentile of the motion
scores of the kept (non-moving) empty-room windows. `--margin` sets the factor on
`detect_live.py --calibrate`, `links_live.py --calibrate` and `evaluate.py`; the default
is `1.25` (`csi.detect.DEFAULT_MARGIN`), and the margin used is printed and recorded in
the calibration.

- `1.0` means the threshold is the 99th percentile of empty windows, so about 1% of
  empty windows can exceed it.
- A **lower** margin catches more brief or weak motion, but raises false alarms: an
  empty room can read MOTION when its scores drift above the calibration clip.
- A **higher** margin (for example `1.5`) is more conservative: fewer false alarms in
  a noisy room, but brief or weak motion may stay EMPTY.

Raise the margin for a noisy room. Compare margins on your own recordings before
choosing one:

```bash
scripts/evaluate.py --margin 1.0 data/<session>
scripts/evaluate.py --margin 1.5 data/<session>
```

Why 1.25: a single empty clip's p99 is crossed by other empty clips (the quiet baseline
drifts a little between clips). On simulated data a margin of 1.0 read MOTION for about 39% of
a second empty clip and 68% of a still breathing person, while 1.25 and 1.5 gave 0%. On the real
single-link `hw5` session walking was 100% MOTION at every margin and empty-room false alarms
rose from 5% (1.5) to 6% (1.25), 11% (1.1) and 14% (1.0). On the 4-link rig a lower margin caught
noticeably more walking on the clean links, so 1.25 is the compromise. Re-check with `evaluate.py`.

**Reset the receiver in place after placing the boards, before calibrating.**
The firmware computes a gain baseline from the first 100 packets after boot, so
a receiver that booted before the boards were placed keeps a baseline from the
old set-up. Place both boards, reset the receiver (its reset button, or unplug
and replug it) without moving anything, then calibrate with the room empty.

**Live state, one line per change:**

```bash
scripts/detect_live.py --port <PORT> --threshold-file calibration.json
```

**One-line live telemetry** (link health, motion score against the threshold,
state, rssi). Use it when you want a compact view of the same stream:

```bash
scripts/telemetry.py --port <PORT> --threshold-file calibration.json
```

Close `detect_live.py`, `telemetry.py` and the recorder before running another,
since only one reader can hold the port.

### Breathing (experimental, opt-in)

The breathing detector is off by default. On real hardware it false-fired on
empty rooms (about 44% of empty windows read `STILL_PRESENT`), so it is not
trusted yet. To turn it on explicitly:

```bash
scripts/detect_live.py --port <PORT> --threshold-file calibration.json --breathing
scripts/evaluate.py --breathing data/<session>      # score the still_* labels too
```

Without `--breathing` the still labels are not scored, and the evaluator
marks them "breathing disabled" instead of reporting a misleading 0%.

## Multiple links (per receiver and sender)

With two senders (S3, C5) and two receivers, there are up to four links, each
identified by receiver and sender MAC (labels such as `rx1<-01`). Each link has
its own signal scale, so each gets its own calibrated threshold:

```bash
# Simulator: 2 senders x 2 receivers, no hardware
scripts/links_live.py --simulate-links
scripts/links_live.py --simulate-links --calibrate 15 --out links.json

# Hardware: name each receiver (unnamed ports become rx1, rx2, ...)
scripts/links_live.py --port rx1=<PORT_A> --port rx2=<PORT_B> --calibrate 60 --out links.json   # empty room
scripts/links_live.py --port rx1=<PORT_A> --port rx2=<PORT_B> --thresholds links.json
```

If one empty clip is too short or someone walked through it, record several clips
in one run (scored separately, pooled; a 3 s pause separates them):

```bash
scripts/links_live.py --port rx1=<PORT_A> --port rx2=<PORT_B> --calibrate 20 --clips 3 --out links.json
```

Loss on real ports is measured against the 4-node rig's 40 Hz sender rate unless
`--rate` says otherwise. A link is refused only when it delivers under 15 packets/s
or loses more than 60% of packets; the reason and the measured rate are printed.

The table refreshes twice a second: state (EMPTY green, MOTION red, NO_SIGNAL grey
when a link has been silent for 2 s), score against threshold, packet rate, drops
and RSSI. A real port needs `--thresholds` (or `--calibrate` first). A link with too
little or too lossy calibration data is reported and gets no threshold, rather than
a guessed one. The single-port scripts above are unchanged.

## Heatmap payload (dashboard state)

`csi.dashboard.DashboardState` turns per-receiver packets into one JSON payload
(`snapshot(now)`): `room`, `nodes`, `links` (state, score, threshold, `normalised` =
score / threshold, pkt rate, drops, rssi, age, `weight`, `contributes`), `heatmap`
(`cols`, `rows`, `extents`, row-major `cells` in 0..1, `max`, `contributing` link ids)
and `status` (links by state, parameters in use). Each link's evidence is
`log(score / threshold)` clipped at 0, so only links above their own threshold add
heat, and quiet links (ratio about 0.65 in the simulator) add none. It is spread along
the sender-receiver segment with a Gaussian falloff (`falloff_m`, default 0.35 m), summed,
soft-saturated (`sensitivity`, default 0.2), smoothed with a 0.3 s rise and a 1.5 s decay
(`rise_tau_s`, `decay_tau_s`) on packet time, and sampled on a 28 x 18 grid (`cols`,
`rows`). Only EMPTY and MOTION links with a threshold contribute. Per-link weights come
from `link_weights` in `config/room.json` (default 1.0; `rx1<-01` is 0.3 because that
diagonal false-alarms on the real rig). This is a rough motion-likelihood estimate, not
a position track.

## Dashboard

`scripts/dashboard.py` runs the multi-link pipeline as a small web server (standard library only). It
serves the heatmap payload over HTTP, so the room page and any script can read it. Open
`http://127.0.0.1:8000/` (or the `--http-port` you chose) in a browser: a room map with the four links coloured by
state, the heatmap, a per-link panel with a 30 s score sparkline, and a status bar. The page works offline, follows
the live stream and reconnects by itself after a restart; Pause freezes the display while the server keeps running.

```bash
# No hardware: a simulated person walks a loop through the room, in real time
scripts/dashboard.py --simulate-room

# Real hardware: name each receiver and reuse a calibration from links_live.py --calibrate
scripts/dashboard.py --port rx1=/dev/cu.usbmodem2101 --port rx2=/dev/cu.usbmodem101 --thresholds data/links_cal1_m1.json
```

- `--http-port` (default 8000), `--host` (default `127.0.0.1`, localhost only; `--host 0.0.0.0` listens on
  the network, with no authentication, so use it only on a network you trust), `--room`, `--baud`, `--seed`.
- Real ports without `--thresholds` still run: their links show `UNCALIBRATED` and `status.messages` says why.
- Ctrl+C stops the server and closes every port.

Endpoints:

- `GET /`: the room page (`web/index.html`, one static file, no network assets).
- `GET /api/state`: the heatmap payload described above, plus a `status` block with `receivers`
  (`connected`, `finished` or `failed` per receiver), `errors`, `finished`, `calibrated_links`,
  `uncalibrated_links` and `messages`.
- `GET /api/events`: the same payload as Server-Sent Events (`data: <json>`), at most about 5 per second.
  The last event has `status.finished` true when the sources end, and then the stream closes.

The library is `csi.server.DashboardServer(room, sources, thresholds)`, where `sources` maps receiver
names to `(host_ts, line)` streams. `start()`, `stop()` and `wait_finished()` manage it.

## Simulator

`--simulate` needs no threshold. It uses a simulator default (1.0) that sits
between simulated empty (about 0.2) and simulated motion (typically 2 or more):

```bash
scripts/detect_live.py --simulate --duration 65
scripts/telemetry.py --simulate
scripts/live.py --simulate
```

## Layout

- `csi/` library: parser, simulator, detectors
- `scripts/` CLIs: live viewer, recorder, live detection, telemetry, offline eval
- `data/` recordings (gitignored)
- `tests/` pytest suite
