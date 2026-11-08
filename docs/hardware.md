# Hardware rig and firmware

This guide covers the physical rig: which boards sit where, how to add boards, how to build and flash the
firmware, and how to find each board's serial port. Running the system (calibrating, the dashboard,
recording) is in [operations.md](operations.md).

[`config/room.json`](../config/room.json) is the source of truth for the rig: nodes, chips, positions,
sender addresses and board serials. Where this guide and the config disagree, the config wins.

## Contents

- [Before you touch a board](#before-you-touch-a-board)
- [The rig](#the-rig)
  - [Nodes](#nodes)
  - [Links](#links)
  - [Link weights](#link-weights)
  - [Placement tips](#placement-tips)
  - [Other room files](#other-room-files)
- [Scaling up](#scaling-up)
  - [How the code treats the layout](#how-the-code-treats-the-layout)
  - [Adding a sender](#adding-a-sender)
  - [Adding a receiver](#adding-a-receiver)
  - [Limits](#limits)
  - [Placement hints](#placement-hints)
- [Flashing the firmware](#flashing-the-firmware)
  - [Setup](#setup)
  - [Flashing with the flash script](#flashing-with-the-flash-script)
  - [Flashing by hand](#flashing-by-hand)
  - [Checking a flashed receiver](#checking-a-flashed-receiver)
  - [Firmware settings and where they live](#firmware-settings-and-where-they-live)
  - [Serial output and parsing](#serial-output-and-parsing)
- [Finding the serial port](#finding-the-serial-port)
  - [Listing boards by serial](#listing-boards-by-serial)
  - [Finding receivers automatically](#finding-receivers-automatically)
  - [Without the helper](#without-the-helper)
  - [Warnings](#warnings)

## Before you touch a board

Three rules cover most of the ways to break a running rig:

1. **A receiver reset makes its thresholds stale.** `csi_recv` takes its gain baseline from the first
   100 packets after boot, so any reset shifts the scores of every link on that receiver. Resets come
   from flashing, any `esptool` command (including `chip_id`), replugging, and (observed) opening a
   native-USB port when a dashboard starts. Calibrate after the last reset.
2. **Identify boards by USB serial, not by port name.** Port names change on every replug and after a
   reset. `python scripts/ports.py` lists every board with its node and opens no port, so it is safe
   while the dashboard runs. See [Finding the serial port](#finding-the-serial-port).
3. **Never run `esptool` against a receiver while the dashboard is running.** `scripts/flash.sh` refuses
   to flash a receiver while a dashboard answers on port 8000.

The agent notes in [`docs/agents/hardware.md`](agents/hardware.md) carry the same rules with the evidence
behind them.

## The rig

Four ESP32-C5 boards around a 2.77 m x 1.84 m room: two receivers in the top corners, forwarding over
ESP-NOW to a gateway board on the dashboard host's USB, and two senders on the bottom wall, on battery or
USB. Coordinates are in metres, x
left to right and y top to bottom, with the origin at the top-left corner. The rig runs on channel 6 and
each sender transmits at 40 Hz.

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
  |                       tx1 sender  tx0  ESP32-C5 sender |
  +--------------------------------------------------------+  y = 1.84
```

### Nodes

| Node | Role | Chip | Position (x, y), m | Sender address | Power |
|---|---|---|---|---|---|
| `rx2` | receiver | ESP32-C5 | (0, 0), top-left | n/a | any USB supply; wireless through the gateway |
| `rx1` | receiver | ESP32-C5 (Seeed XIAO) | (2.77, 0), top-right | n/a | any USB supply; wireless through the gateway |
| `tx0` | sender | ESP32-C5 (Seeed XIAO) | (2.77, 1.84), bottom-right | `1a:00:00:00:00:00` (`SENDER_ID` 0x00) | battery or USB |
| `tx1` | sender | ESP32-C5 | (1.385, 1.84), bottom-middle | `1a:00:00:00:00:01` (`SENDER_ID` 0x01) | battery or USB |

The Seeed XIAO boards need their external antenna, pressed on until it clicks. Keep every receiver at least
1 m from the gateway; the reason, and how to read a receiver's or the gateway's output, are in
`docs/agents/hardware.md`, "Wireless receivers".

Each node also carries `"serial"`: the board's USB serial, which equals its base MAC (for example
`38:44:BE:A5:18:B0`). That is how `scripts/ports.py` and the dashboard's `--auto-ports` recognise a board.
Read the values in `config/room.json`.

A third receiver, `rx3` (ESP32-C5, top-middle at (1.385, 0)), is defined in `config/room.json` with
`"enabled": false`. It is not part of the rig and is ignored everywhere until you set it to `true` (see
[Adding a receiver](#adding-a-receiver)). It has no `serial` yet. It was tried as a fifth node on
2026-11-08 and taken out again; its board (`10:BD:A3:CB:E6:4C`) is now the gateway.

### Links

Every enabled receiver hears every enabled sender, so the rig has four links. A link is labelled
`<receiver><-<last byte of the sender address>`.

| Link | Path across the room | Notes |
|---|---|---|
| `rx1<-00` | right wall | |
| `rx1<-01` | diagonal, top-right to bottom-middle | Called the "long diagonal" in older notes, from when `tx1` sat bottom-left. |
| `rx2<-00` | diagonal, top-left to bottom-right | Called the "C5 diagonal" in older notes. |
| `rx2<-01` | diagonal, top-left to bottom-middle | |

On 2026-11-08, through the gateway, every link ran at 37 to 40 packets/s. Earlier, with wired receivers
and `tx0` still an ESP32-S3, every link ran at 36 to 38 packets/s with about 5% loss.

In the 2026-11-07 calibration, before `tx1` moved, `rx1<-01` dropped to about 10 packets/s with RSSI -57.
Check the packet rate and drops on a link's dashboard card before trusting its threshold.

### Link weights

`link_weights` in `config/room.json` is empty, so every link counts fully on the heatmap. Weights affect
the heatmap only, never a link's EMPTY / MOTION state. It used to hold `"rx1<-01": 0.3`, set when `rx1` was
an ESP32-S3 receiver whose long-diagonal link false-alarmed. With `rx1` a C5, that link was the cleanest on
the rig (2026-11-08, on the Pi: -45 dBm, 38 packets/s, no evidence at its median score, full evidence when
someone moved), so the down-weighting was removed.

### Placement tips

- Mount each board fixed, at roughly chest height, with its antenna facing into the room.
- Keep metal out of the line between boards.
- Use ESP32-C5 receivers. In quiet-room captures, links on an ESP32-S3 receiver read MOTION 35 to 65% of
  the time, against 4 to 16% for a C5 receiver. ESP32-S3 and ESP32-C5 senders both work. The original
  ESP32 was noisy both as receiver and as sender, and is retired.
- After placing the boards, reset each receiver in place before calibrating (see
  [Calibrating](operations.md#calibrating)).

The first single-link recordings (`hw3`, `hw5`) used one C5 receiver and one S3 sender about 2 m apart.
That setup is retired; the layout above is the current rig.

### Other room files

- `config/room_c5_only.json` is an earlier variant: only `rx2` is enabled (`rx1` and `rx3` are disabled,
  and `rx3` sits 1.5 m beyond the top wall), and it lists `rx1`, `rx3` and `tx0` as ESP32-S3. It does not
  describe the current rig.
- `tests/fixtures/` holds layouts for tests and the simulator; see [Placement hints](#placement-hints).
- Some of these files still carry a `port_hint` field. Nothing reads it: boards are found by `serial`.
- The room file defaults to `config/room.json` (the `--room` option of `dashboard.py`, `links_live.py`
  and `ports.py`), or to the file named by the `CSI_ROOM` environment variable when it is set. The test
  suite sets `CSI_ROOM` to a frozen fixture; make sure it is not set in the shell you run the rig from.

## Scaling up

The rig is defined in data. Nodes live in `config/room.json` under `"nodes"`, and the links are every
enabled receiver paired with every enabled sender. Adding a board means a config entry, a flash and a
recalibration; no code changes.

### How the code treats the layout

- **Links.** One per (receiver, sender) pair: 3 receivers and 2 senders give 6 links, 3 and 3 give 9.
  Check the link list on the simulator before any hardware is plugged in:

  ```bash
  python scripts/links_live.py --simulate-links --duration 2                                     # config/room.json
  python scripts/links_live.py --simulate-links --room tests/fixtures/room_5node.json --duration 2
  ```

- **Disabled nodes.** `"enabled": false` removes a node completely: no links, nothing in the dashboard
  payload, and its serial port is never opened, even if one is passed with `--port`. A disabled node's
  board may be missing, so a node can be defined before its board is plugged in.
- **Positions.** A node may sit outside the room rectangle. The map frames the room and every node, and a
  node outside the room gets a dashed leader and an "outside room" label. The heatmap covers the room
  only.
- **Link weights** are keyed by link label, for example `"rx1<-01": 0.3`. A link without an entry has
  weight 1.0.

### Adding a sender

1. Plug the new board in by USB and run `python scripts/ports.py`. It shows up as `NO NODE`; note its
   port and its serial.
2. Pick the next unused `SENDER_ID` (`0x02` for a third sender). Its address becomes `1a:00:00:00:00:02`,
   which the receivers' filter already accepts. Every sender needs a unique ID: two senders with the
   same ID merge into one link that looks like one noisy sender.
3. Build and flash it (see [Flashing with the flash script](#flashing-with-the-flash-script)):

   ```bash
   scripts/flash.sh sender esp32c5 <PORT> 0x02
   ```

   Channel and send rate are compiled in, so the new sender matches the others. With three or more
   senders, lower the send rate first (see [Limits](#limits)).
4. Power it from battery or USB.
5. Add a sender node to `config/room.json` with its `role`, `chip`, `mac`, `x`, `y` and `serial`.
6. Calibrate again (see [Calibrating](operations.md#calibrating)). The new links have no threshold until
   you do, and show UNCALIBRATED.

### Adding a receiver

1. Stop the dashboard. `scripts/flash.sh` refuses to flash a receiver while one answers on port 8000.
2. Plug the new board into its own USB port and run `python scripts/ports.py` to find its port and
   serial.
3. Flash `csi_recv` for an ESP32-C5:

   ```bash
   scripts/flash.sh receiver esp32c5 <PORT>
   ```

4. Add a receiver node to `config/room.json` with its `serial`, or set `"enabled": true` on one that is
   already there. `rx3` has no `serial` yet, so add one when you enable it; without it `--auto-ports`
   fails naming `rx3`.
5. Start the dashboard. `--auto-ports` finds every enabled receiver by its serial; the other tools need
   each port named with `--port rxN=<PORT>`:

   ```bash
   python scripts/dashboard.py --auto-ports --thresholds links.json
   python scripts/dashboard.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --port rx3=<PORT_RX3> --thresholds links.json
   python scripts/links_live.py --port rx1=<PORT_RX1> --port rx2=<PORT_RX2> --port rx3=<PORT_RX3> --calibrate 10 --clips 3 --out links.json
   ```

6. Reset the receivers in place and calibrate with the room empty. Only then are thresholds for the new
   links meaningful.

### Limits

- **One channel, shared by all senders.** Collisions grow with the number of senders. Two senders at
  100 Hz lost about 65% of packets; two at 40 Hz lose about 5 to 10%. With three senders, use about 30 Hz
  or lower: change `CONFIG_SEND_FREQUENCY` in `firmware/csi_send/main/app_main.c` and reflash every
  sender.
- **One USB port per receiver.** Each receiver needs its own serial port.
- **Receivers must be ESP32-C5.** On this rig, a third S3 receiver cut the other receivers' packet rate
  from about 35 to 22 to 26 packets/s. A third C5 receiver did not: all links stayed at 34 to 38
  packets/s.
- **Receivers are wired.** Wireless receivers (CSI forwarded over UDP) are not built; the decision to
  stay wired for now (2026-11-07) and its estimate are in
  [ADR 0004](adr/0004-wired-receivers-wireless-deferred.md).

### Placement hints

From one walk test on this rig: a C5 receiver on the top wall between the two top corners was the
strongest sensor, with walking scoring 25 to 31 against a quiet maximum of about 11. The right-side links
from a corner receiver can respond weakly, so check each link's score during a walk before trusting its
threshold.

Two layout examples for tests and the simulator live in `tests/fixtures/`: `room_5node.json` (three
receivers, two senders, `rx3` on the top wall at (1.385, 0)) and `room_star.json` (`rx3` 1.5 m beyond the
top wall). Both list `rx1`, `rx3` and `tx0` as ESP32-S3, which predates the receiver-chip finding and the
move to C5 boards, so treat them as layouts, not as a parts list.

## Flashing the firmware

The firmware is vendored in [`firmware/`](../firmware/): `csi_recv` (receiver) and `csi_send` (sender),
Espressif's [esp-csi](https://github.com/espressif/esp-csi) get-started examples at upstream commit
`8633d67`, with our edits. There is nothing to clone. [`firmware/README.md`](../firmware/README.md) is
the reference for building by hand; this section covers the rig procedure and `scripts/flash.sh`.

Before flashing, read [Before you touch a board](#before-you-touch-a-board). Flashing a receiver resets
it and makes every saved threshold on its links stale. Flashing a sender leaves the receivers' baselines
alone, but expect its links to change.

### Setup

ESP-IDF stays outside the repository. The firmware is verified with **ESP-IDF v5.5.6**; the ESP32-C5
needs v5.5 or newer. Install it per the
[ESP-IDF v5.5.6 guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/) (the team keeps a clone at
`~/esp/esp-idf`), then in every shell you build from:

```bash
. ~/esp/esp-idf/export.sh        # or: . $IDF_PATH/export.sh
```

### Flashing with the flash script

`scripts/flash.sh` builds one board's firmware, checks that the port holds the chip you named, and
flashes it:

```
scripts/flash.sh <receiver|sender> <esp32c5|esp32s3> <port> [sender_id] [--force] [--dry-run]
```

For the current rig, where every board is an ESP32-C5:

```bash
python scripts/ports.py                                   # find the board's port by its serial
scripts/flash.sh --dry-run receiver esp32c5 <PORT>        # print the commands, run none
scripts/flash.sh receiver esp32c5 <PORT>                  # rx1, rx2 or any new receiver
scripts/flash.sh sender esp32c5 <PORT> 0x00               # tx0
scripts/flash.sh sender esp32c5 <PORT> 0x01               # tx1
```

| Argument or option | Meaning |
|---|---|
| `receiver` or `sender` | Builds `firmware/csi_recv` or `firmware/csi_send`. |
| `esp32c5` or `esp32s3` | The target chip. No other chip is accepted. |
| `<port>` | The board's serial port, from `scripts/ports.py`. |
| `sender_id` | Required for a sender, not allowed for a receiver. Exactly `0x` and two hex digits, for example `0x01`. |
| `--dry-run` | Print every command, prefixed with `+`, and run none of them. The read-only dashboard probe still runs. |
| `--force` | Flash a receiver even though a dashboard answers. You accept the re-baseline. |
| `-h`, `--help` | Print usage. |
| `FLASH_DASHBOARD_URL` (environment) | The dashboard URL to probe. Default `http://127.0.0.1:8000`. |

What the script does, in order, and where it stops:

1. **Checks the arguments.** Any usage error exits with status 2.
2. **Checks the toolchain.** `IDF_PATH` must be set and `idf.py` must be on `PATH`. The script sources
   nothing, so run `export.sh` first. This check runs under `--dry-run` too.
3. **Receivers only: refuses while a dashboard runs.** It probes the dashboard URL with `curl` (2 s
   timeout). If anything answers, it explains the re-baseline and exits with status 1, unless `--force`
   is given. The probe also runs under `--dry-run`, so a dry run for a receiver is refused while a
   dashboard runs. Senders are never refused.
4. **Builds** with `idf.py -C <example> -B <build dir> -DSDKCONFIG=<build dir>/sdkconfig set-target <chip>`,
   then `build` (a sender also gets `-DSENDER_ID=<id>`). The build directory is
   `firmware/csi_recv/build_<chip>` for a receiver and `firmware/csi_send/build_<chip>_id<NN>` for a
   sender, for example `build_esp32c5_id01`. Each build directory keeps its own `sdkconfig`, so chips
   and sender IDs never share a configuration. These names do not collide with the hand-built
   `build_c5` directories in `firmware/README.md`.
5. **Stops if the build left no `flash_args`.** Nothing is flashed.
6. **Checks the chip.** `python -m esptool --port <PORT> chip_id` must report the chip you named (for
   example `ESP32-C5`). If esptool cannot read a chip, or reads a different one, the script exits with
   status 1 and nothing is flashed. This read resets the board.
7. **Flashes** with `python -m esptool --port <PORT> write_flash @flash_args`, run from inside the build
   directory.

Exit status: 0 done, 1 refused or failed, 2 usage error. `flash.sh` is a bash script for macOS and Linux
shells; it needs `curl`, and `python -m esptool` must work in the shell (the ESP-IDF environment provides
esptool).

Building runs `set-target`, which rewrites `firmware/*/dependencies.lock`. Read the note on that file in
`firmware/README.md` before committing.

### Flashing by hand

To run the `idf.py` steps yourself, follow "Build and flash" in
[`firmware/README.md`](../firmware/README.md). The rules that matter:

- Use one build directory (`-B`) per chip and role, and per `SENDER_ID` for senders, and run
  `set-target` in it. The `sdkconfig` carries the last target, and a directory configured for one chip
  will not boot on another. We once mixed up targets in a shared build directory; keep them apart.
- Building two chips from one example folder needs `-DSDKCONFIG=<build dir>/sdkconfig` on each call.
- Changing `SENDER_ID` in an existing build directory does not reliably rebuild; use a fresh directory.
- Run `esptool write_flash @flash_args` from inside the build directory.

No ESP32-S3 receiver is in use, so an S3 receiver build is only needed if you bring one back, which is
not recommended (see [Placement tips](#placement-tips)).

### Checking a flashed receiver

Start the dashboard (or `scripts/links_live.py`) and check that each of the receiver's links shows a
steady packet rate. To read the raw output instead, run `idf.py` with the same `-C`, `-B` and
`-DSDKCONFIG` arguments the build used (`flash.sh` prints them), plus `-p <PORT> monitor`, and look for
`CSI_DATA` lines.
Exit the monitor with Ctrl+]. Close it before you run any Python script: only one program can hold a
port. Opening the port can reset the receiver, so calibrate afterwards.

### Firmware settings and where they live

| Setting | Value on the rig | Where it is set |
|---|---|---|
| Channel | 6, on senders and receivers | `#define CONFIG_LESS_INTERFERENCE_CHANNEL` in `firmware/csi_send/main/app_main.c` and `firmware/csi_recv/main/app_main.c`. It is hard-coded, so the menuconfig channel alone does not change it. Set the same channel on every board and check that the `CSI_DATA` lines report it. |
| Send rate | 40 Hz | `#define CONFIG_SEND_FREQUENCY` in `firmware/csi_send/main/app_main.c`. At 100 Hz two senders lost about 65% of packets. |
| Sender address | `1a:00:00:00:00:<SENDER_ID>`, default `0x00` | Build flag `-DSENDER_ID=0xNN` (`firmware/csi_send/CMakeLists.txt`); `flash.sh` passes it. |
| Receiver filter | Any sender whose first 5 address bytes are `1a:00:00:00:00` | A `memcmp` in `firmware/csi_recv/main/app_main.c`, so one receiver hears every sender. |
| Receiver gain | Locked: `CONFIG_FORCE_GAIN` 1; baseline from the first 100 packets after boot | `firmware/csi_recv/main/app_main.c` |
| Console baud | 921600 | `CONFIG_ESP_CONSOLE_UART_BAUDRATE` in `firmware/csi_recv/sdkconfig.defaults`. Every Python tool defaults to `--baud 921600`. |

### Serial output and parsing

- The receiver prints one `CSI_DATA` line per packet. The column layout differs per chip. The parser
  (`csi/parser.py`) reads the header line that `csi_recv` prints at boot; when a port is opened after
  boot and the header is missed, it matches the column count against the known layouts (including the
  C5's). No per-chip setting is needed. If a line fails to parse, check the header first.
- If you change the printed columns in the firmware, change the parser and its tests with it.
- Close `idf.py monitor` before running any Python script. Only one program can hold a port.

## Finding the serial port

Port names (`/dev/cu.usbmodem101`, `COM3`) change on every replug and after a reset, so never reuse one
from an earlier run. Boards are identified instead by their USB serial, which equals the board's base MAC
and is stored as `"serial"` on each node in `config/room.json`.

### Listing boards by serial

```bash
python scripts/ports.py                      # room from config/room.json
python scripts/ports.py --room other.json
```

`scripts/ports.py` lists every connected USB serial device (non-USB entries such as Bluetooth ports are
skipped) in three columns, `PORT`, `SERIAL` and `NODE`, then flags anything that does not match:

| Output | Meaning |
|---|---|
| `NO NODE (not in the room config: ...)` in the NODE column | A connected board that no node names: a board you forgot, or one to add. Its SERIAL is the value for `config/room.json`. |
| `NOT CONNECTED: <node> (serial ...)` | A configured node whose board is not plugged in. |
| `NO SERIAL: <nodes>` | Nodes with no `serial` in the config (today, `rx3`). |

It uses `serial.tools.list_ports` only: no port is opened, so no board resets, and it is safe to run
while the dashboard holds the ports. The exit code is always 0; read the flags. Serials are compared
ignoring case and separators, so `38:44:be:a5:18:b0` and `3844BEA518B0` are the same board.

### Finding receivers automatically

`--auto-ports` on the dashboard does the same lookup for every enabled receiver and uses the ports it
finds:

```bash
python scripts/dashboard.py --auto-ports --thresholds links.json
```

It fails before opening any port, naming each receiver that has no `serial` in the config, is not
connected, or whose serial matches more than one port. It replaces `--port` and cannot be combined with
it. Only `scripts/dashboard.py` has `--auto-ports`; the terminal tools (`links_live.py`, `detect_live.py`,
`telemetry.py`, `live.py`, `record.py`) take `--port`, so copy the paths from `scripts/ports.py`.

The rig is run from macOS. `ports.py` and `--auto-ports` rely on the USB serial number that pyserial
reports; on Linux or Windows, check that `ports.py` shows your boards' serials before relying on
`--auto-ports`.

### Without the helper

| OS | Command | Typical name |
|---|---|---|
| macOS | `ls /dev/cu.*` | `/dev/cu.usbmodem101`, `/dev/cu.usbmodem2101` |
| Linux | `ls /dev/ttyUSB* /dev/ttyACM*` | `/dev/ttyUSB0`, `/dev/ttyACM0` (may need `sudo usermod -aG dialout $USER`, then log in again) |
| Windows | Device Manager, Ports (COM & LPT) | `COM3` |
| Any | `python -m serial.tools.list_ports -v` | Lists every port with its description and serial; opens nothing. |

On macOS use the `cu.*` devices, never `tty.*`. Unplug and replug a board to see which entry appears, and
use the receiver's port, not the sender's.

### Warnings

- **Reading a chip ID with esptool resets the board.** So does every other esptool command.
- **Never probe a receiver's port while the dashboard or any other reader uses it.**
- **Resetting a receiver re-records its gain baseline** (from the first 100 packets after boot) **and
  shifts its thresholds.** Calibrate after the last reset.
- **Only one program can hold a port.** Close `idf.py monitor`, `record.py` and the other readers before
  starting the dashboard.
