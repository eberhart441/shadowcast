# Firmware

ESP32 firmware for the CSI rig: `csi_send` transmits packets, `csi_recv` measures channel state information (CSI) on them and streams it over USB serial to `scripts/dashboard.py`. Both are Espressif's stock `esp-csi` get-started examples with a few edits, vendored here so the repo is self-contained. Upstream: https://github.com/espressif/esp-csi at commit `8633d67`.

Which board is which (chip, position, sender address, port hint) is in `config/room.json`. Read it there.

## Our edits to the stock examples

- Channel 6 (`CONFIG_LESS_INTERFERENCE_CHANNEL`), sender and receiver.
- Sender: send frequency 40 Hz; the last byte of its address is `SENDER_ID`, default `0x00`, set at build time (below).
- Receiver: `CONFIG_FORCE_GAIN` 1, and it accepts any sender whose first 5 address bytes are `1a:00:00:00:00`, so one receiver hears every sender.
- Receiver, unchanged from stock: it also sets its own address to `1a:00:00:00:00:00`.

## Setup (once per machine)

ESP-IDF 5.5.6, kept outside the repo. Install per https://docs.espressif.com/projects/esp-idf/en/v5.5.6/ (a clone at `~/esp/esp-idf` is what the team uses; its compilers and tools install to `~/.espressif`). Targets used: `esp32c5` and `esp32s3`. Then in each shell:

```
. ~/esp/esp-idf/export.sh
```

## Build and flash

Use one build directory per chip and role, set up with `-B`. The `sdkconfig` is generated and carries the last target, so a build directory configured for one chip will not boot on another.

Building two chips in parallel from one example folder needs a separate config per build directory: add `-DSDKCONFIG=<build_dir>/sdkconfig` to each `idf.py` call. The example's `sdkconfig` is shared and carries the last target.

Receiver (any chip; C5 is the one to use, see `docs/agents/hardware.md`):

```
cd firmware/csi_recv
idf.py -B build_c5 set-target esp32c5
idf.py -B build_c5 build
idf.py -B build_c5 -p /dev/cu.usbmodemXXXX flash
```

Sender: pick the ID from `config/room.json` (the last byte of the sender's `mac`, e.g. tx0 is `0x00`, tx1 is `0x01`) and give each ID its own build directory:

```
cd firmware/csi_send
idf.py -B build_c5_id01 -DSENDER_ID=0x01 set-target esp32c5
idf.py -B build_c5_id01 build
idf.py -B build_c5_id01 -p /dev/cu.usbmodemXXXX flash
```

Use `esp32s3` instead of `esp32c5` for an S3 board. Changing `SENDER_ID` in an existing build directory does not reliably rebuild; use a fresh directory.

`flash_args` lists paths relative to the build directory, so run `esptool write_flash @flash_args` from inside it, not from `firmware/`.

`dependencies.lock` records the last `set-target`. After building another chip, run `git checkout -- firmware/*/dependencies.lock` so the target change is not committed.

`scripts/flash.sh <receiver|sender> <esp32c5|esp32s3> <port> [sender_id]` does the build, the check that the port holds the named chip, and the flash for one board, in `build_<chip>` (or `build_<chip>_id<NN>` for a sender). It needs `IDF_PATH` (source `export.sh` first). It refuses a receiver while a dashboard answers on port 8000, unless `--force` is given. `--dry-run` prints every command and runs none.

## Wireless receivers (no USB per receiver)

Two build flags on `csi_recv`, both off by default (off is the wired receiver above):

- `CSI_FORWARD=1`: a receiver sends each CSI line over ESP-NOW (v2, up to 1470 bytes) to the gateway instead of
  printing it, prefixed `RX,<its USB serial>,`. It stays on the sensing channel with the same radio settings, so
  the CSI is unchanged. Its own address becomes `1a:00:00:00:01:<last serial byte>`, so no receiver mistakes it
  for a sender. Power it from any USB charger or battery.
- `CSI_GATEWAY=1`: the one board on the Pi's USB. It measures nothing; it writes every forwarded line to USB
  (address `1a:00:00:00:02:00`).

```
cd firmware/csi_recv
idf.py -B build_c5_fwd -DCSI_FORWARD=1 set-target esp32c5 && idf.py -B build_c5_fwd build   # every receiver
idf.py -B build_c5_gw -DCSI_GATEWAY=1 set-target esp32c5 && idf.py -B build_c5_gw build     # the gateway
python scripts/dashboard.py --gateway <GATEWAY_PORT> --thresholds links.json
```

The dashboard tells receivers apart by the `serial` of each receiver in `config/room.json`, so every enabled
receiver needs one. Senders are unchanged.

## Serial output

The receiver prints one `CSI_DATA` line per packet at 921600 baud (set in `csi_recv/sdkconfig.defaults`; `csi/parser.py` `serial_stream` opens ports at the same rate). The column layout is `C5_HEADER` in `csi/parser.py`, which the receiver also prints once at boot. If you change the printed columns, change the parser and its tests with it.

## Before you flash a receiver

Flashing resets the board, and a receiver takes its gain baseline from the first 100 packets after boot, so every saved threshold on that receiver's links goes stale. Recalibrate after the last reset and read `docs/agents/hardware.md` first. A sender can be flashed without affecting receivers' baselines, but expect its links to change.

- Flash only one port at a time and confirm it is the right board: list ports with `python -m serial.tools.list_ports -v`, which has no side effects. A port name can change after a reset.
- Never run `esptool` (including `chip-id`) on a receiver while the dashboard is running.
- macOS: use `/dev/cu.*` ports.

## Not in the repo

Build output (`build*/`), `sdkconfig`, and `managed_components/` are gitignored and regenerated by `idf.py`. The `esp_csi_gain_ctrl` component downloads on the first build.
