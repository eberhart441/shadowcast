# Firmware

ESP32 firmware for the CSI rig: `csi_send` transmits packets, `csi_recv` measures channel state information (CSI) on them and streams it over USB serial to `scripts/dashboard.py`. Both are Espressif's stock `esp-csi` get-started examples with a few edits, vendored here so the repo is self-contained. Upstream: https://github.com/espressif/esp-csi at commit `8633d67`.

Which board is which (chip, position, sender address, board serial) is in `config/room.json`. Read it there.

The transceiver rig of spec #2 runs its own image, `node/` (below, "Node image"). `csi_send` and `csi_recv` stay for
the hackathon rig.

## Node image (transceiver rig)

`node/` is one image for every board of the transceiver rig, ESP32-C5 only ([ADR-0026](../docs/adr/0026-one-node-image-runtime-settings-gateway-records.md)).
There are no build flags and no per-board build directories: what a board does is its node settings, set over USB
after flashing.

```
cd firmware/node
idf.py -B build set-target esp32c5
idf.py -B build build                                   # ESP-IDF 6.0; CI builds the same in espressif/idf:v6.0
cd build && esptool --chip esp32c5 -p <PORT> -b 460800 write-flash @flash_args   # unsigned: never takes an OTA
# for over-the-air updates flash a signed image instead (docs/ota.md "Once per board")
# a transceiver needs no settings; to change the kit-wide ones, give every node the same values:
python scripts/node.py <PORT> set channel=6 round_hz=40 guard_us=250
python scripts/node.py <PORT> set role=gateway channel=6        # the board on the dashboard host
python scripts/node.py keygen                                     # once per kit: data/kit.key
python scripts/node.py <PORT> key data/kit.key                    # every transceiver
python scripts/dashboard.py --node-gateway <GATEWAY PORT> --room config/room_tr2.json
```

- **Transceiver:** the round scheduler (`core/round_scheduler`) gives it a node index and a slot: it listens,
  joins the round it hears (or founds one alone), and sends its round broadcast
  ([`docs/round-broadcast.md`](../docs/round-broadcast.md)) in its slot as an unacknowledged ESP-NOW broadcast at
  HT20, MCS0. It measures CSI on the others' round broadcasts (HT20 only), scores each link with
  `core/motion_score` and carries the link reports. Every frame it sends is signed with the kit key, and a heard
  one whose tag or round number fails is ignored (`main/node_auth.c`, mbedTLS on the SHA accelerator). It needs
  no per-board setting, only the kit key: without it, it only listens.
- **Gateway:** listens only and writes every frame it hears to USB as a heard record with its receive time.
- **USB carries only gateway records** ([`docs/gateway-record.md`](../docs/gateway-record.md)): status once a
  second, text (log lines and command replies), heard records on a gateway. Read a node with
  `python scripts/node.py <PORT> status`, not a serial terminal. Nothing goes to the UART after boot, and no CSI is
  ever formatted as text.
- **Gain** is the hackathon receiver's (record 100 frames, then force their median gain) until ticket #15.
- **Over-the-air updates** ([`docs/ota.md`](../docs/ota.md), [ADR-0030](../docs/adr/0030-ota-maintenance-network-signed-images.md)):
  two 1.875 MB app slots (`node/partitions.csv`, 4 MB flash), rollback on a failed health check, and only images
  signed with the kit's signing key. The build makes an **unsigned** `node.bin` ("App built but not signed"); sign
  it with `espsecure sign-data --version 2 --keyfile data/ota-signing.pem` and flash or roll out the signed file
  (`scripts/ota.py`). A board running an unsigned image refuses every update. The update code is
  `node/main/node_ota.c` over `core/ota_core.c`; with it the image is 0x110000 B (padded to 64 KB for signing), 43 %
  of a slot free, and the bootloader 19 % free (ESP-IDF 6.0).
- The logic that does not need ESP-IDF is in `core/` and tested on the PC; `node/main/node_main.c` is only the glue
  (Wi-Fi, ESP-NOW, CSI callback, timer, NVS, USB).
- The two-board rig check is [`docs/rig-checks/two-nodes.md`](../docs/rig-checks/two-nodes.md); the round
  scheduler's (unplug and replug with 3+ boards) is [`docs/rig-checks/round-scheduler.md`](../docs/rig-checks/round-scheduler.md).

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

## Node core (portable C)

`core/` holds firmware logic that is plain C11 with no ESP-IDF calls, so it builds and is tested on a PC:
`core/motion_score.{h,c}` is the on-node motion score and score code ([ADR-0021](../docs/adr/0021-on-node-score-streaming-window-and-score-code.md)).
Run its tests with `firmware/core/test.sh` (any C compiler; `scripts/check.sh` runs it too, and CI runs it with gcc).
They replay `core/tests/vectors/motion_score.txt`, which `scripts/gen_score_vectors.py` writes from the Python scorer.

The node image's other core modules, each with a test in `core/tests/`:

| Module | What it does |
|---|---|
| `round_broadcast` | Round broadcast v2 encoder and decoder. Tests replay `tests/vectors/round_broadcast.txt`, flattened from the host's golden fixture `tests/fixtures/round_broadcast_v2.json` by `scripts/gen_wire_vectors.py`. |
| `round_auth` | The authentication tag (HMAC-SHA256 from a function the platform supplies), the freshness rule against replay, the key check value. Tests replay `tests/vectors/round_auth.txt` (same fixture) with a reference HMAC-SHA256 (`tests/hmac_sha256.c`, checked against RFC 4231). |
| `gateway_record` | Gateway record v1 encoder (and decoder, for the tests). Same, from `tests/fixtures/gateway_record_v1.json`. |
| `espnow_frame` | The ESP-NOW body (the round broadcast) inside the frame the CSI callback hands over. |
| `csi_layout` | One amplitude per kept subcarrier, nulls learned as `csi/parser.py` learns an unknown layout. |
| `link_table` | Per-transmitter link state and the link reports (calls `motion_score_get` once per link per round). |
| `round_scheduler` | The round scheduler: join, drop, failover and merge, slot timing from any heard round broadcast, slot lateness ([ADR-0027](../docs/adr/0027-round-scheduler.md)). Tests run up to 30 nodes over a simulated radio. |
| `node_settings` | The runtime settings and their text commands. |

`tests/test_wire_vectors.py` fails when a flattened vector file no longer matches its JSON fixture: rerun
`scripts/gen_wire_vectors.py`.

## Not in the repo

Build output (`build*/`), `sdkconfig`, and `managed_components/` are gitignored and regenerated by `idf.py`. The `esp_csi_gain_ctrl` component downloads on the first build.
