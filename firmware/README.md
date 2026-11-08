# Firmware

ESP32 firmware for the CSI rig: `csi_send` transmits packets, `csi_recv` measures channel state information (CSI) on them and streams it over USB serial to `scripts/dashboard.py`. Both are Espressif's stock `esp-csi` get-started examples with a few edits, vendored here so the repo is self-contained. Upstream: https://github.com/espressif/esp-csi at commit `8633d67`.

Which board is which (chip, position, sender address, port hint) is in `config/room.json`. Read it there.

## Our edits to the stock examples

- Team address prefix `1a:0c:12:00:00`, defined once in each `app_main.c` as `CSI_MAC_PREFIX` (keep the two equal). A sender's address is the prefix plus its `SENDER_ID` (`1a:0c:12:00:00:<id>`). Every receiver sets its own address to `1a:0c:12:00:00:fe` and accepts any sender whose first 5 address bytes are the prefix, so one receiver hears every sender and ignores stock esp-csi boards (`1a:00:00:00:00:xx`).
- Channel 6, HT40 with the secondary channel below, sender and receiver (build flag `CSI_CHANNEL`).
- Sender: 20 Hz (build flag `CSI_SEND_HZ`).
- Receiver: `CONFIG_FORCE_GAIN` 1 (build flag `CSI_FORCE_GAIN`). The per-packet `compensate_gain` log line is now debug level, so it is no longer printed; it added about 80 bytes per packet to the console.

## Setup (once per machine)

ESP-IDF 5.5.6, kept outside the repo. Install per https://docs.espressif.com/projects/esp-idf/en/v5.5.6/ (a clone at `~/esp/esp-idf` is what the team uses; its compilers and tools install to `~/.espressif`). Targets used: `esp32c5` and `esp32s3`. Then in each shell:

```
. ~/esp/esp-idf/export.sh
```

## Build and flash

Use one build directory per chip, role and set of build flags, set up with `-B`. The `sdkconfig` is generated and carries the last target, so a build directory configured for one chip will not boot on another.

Pass build flags on the `set-target` line of a new build directory, as below. They stay in that directory's CMake cache. Changing or dropping a flag in an existing directory does not reliably rebuild, so use a fresh directory for each combination.

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

Use `esp32s3` instead of `esp32c5` for an S3 board. Confirm the image from the sender's boot line (`wifi_channel: 6, send_frequency: 20, mac: 1a:0c:12:00:00:01`), shown when you flash with `idf.py -B build_c5_id01 -p /dev/cu.usbmodemXXXX flash monitor` (Ctrl-] quits).

Images can be built ahead of time, but all build directories of one project share its `sdkconfig`, and each `set-target` rewrites it. Build each image straight after its `set-target`. To flash a pre-built image later without rebuilding it (`idf.py flash` refuses when the shared `sdkconfig` was last set up for another chip), run the `esptool` command that `idf.py` prints at the end of the build, from the build directory:

```
cd build_c5_id01
python -m esptool --chip esp32c5 -p /dev/cu.usbmodemXXXX -b 460800 --before default_reset --after hard_reset write_flash "@flash_args"
```

## Build flags

| Flag | Firmware | Default | What it does |
|---|---|---|---|
| `SENDER_ID` | `csi_send` | `0x00` | Last byte of the sender address `1a:0c:12:00:00:<id>`. One per sender. `0xfe` is refused: it is the receivers' byte. |
| `CSI_SEND_HZ` | `csi_send` | `20` | Nominal packets per second. The real rate is a little lower (see the budget below). |
| `CSI_CHANNEL` | both | `6` | Wi-Fi channel, 5 to 11. Every board must use the same one. |
| `CSI_PRINT_STRIDE` | `csi_recv` | `1` | Print only every Nth subcarrier (I/Q pair). `4` prints 30 of the C5's 117 subcarriers (`len` 60), and a line shrinks from about 0.85 KB to about 0.27 KB. |
| `CSI_FORCE_GAIN` | `csi_recv` | `1` | `1` forces the receive gain at packet 100 (stock). `0` leaves the AGC running; use it with `CSI_PRINT_RAW=1`. |
| `CSI_PRINT_RAW` | `csi_recv` | `0` | `1` prints the raw int8 CSI values, without the gain-compensation multiply. |
| `CSI_GAIN_LOCK_STRONGEST` | `csi_recv` | `0` | Experimental. `1` forces the strongest sender's gain instead of the median gain. |
| `CSI_GAIN_LOCK_OFFSET` | `csi_recv` | `0` | With the lock: lower the locked AGC by this many steps (about 1 dB each) for headroom. |

A bad value (channel outside 5 to 11, stride below 1, rate below 1, `SENDER_ID` `0xfe` or above `0xff`, a gain lock without forced gain or on a chip without gain control) stops the build with an error. Examples, each followed by `idf.py -B <dir> build` and a flash:

```
cd firmware/csi_recv
idf.py -B build_c5_ch11 -DCSI_CHANNEL=11 set-target esp32c5                                    # channel-11 fallback
idf.py -B build_c5_stride4 -DCSI_PRINT_STRIDE=4 set-target esp32c5                             # every 4th subcarrier
idf.py -B build_c5_lock2 -DCSI_GAIN_LOCK_STRONGEST=1 -DCSI_GAIN_LOCK_OFFSET=2 set-target esp32c5 # experimental gain lock
idf.py -B build_c5_agc_raw -DCSI_FORCE_GAIN=0 -DCSI_PRINT_RAW=1 set-target esp32c5             # AGC on, raw values (A/B only)

cd ../csi_send
idf.py -B build_s3_id02_25hz -DSENDER_ID=0x02 -DCSI_SEND_HZ=25 set-target esp32s3
```

### Gain options

Stock behaviour (`CSI_FORCE_GAIN=1`): the receiver takes the median AGC gain of the first 50 packets after boot, from all senders mixed, and forces it at packet 100. A sender much stronger than that median clips at the int8 rail (scaled from our hw5 data: about 29% of subcarriers at 6 dB stronger); a much weaker one gets coarse values. Every reset picks a new gain, which shifts that receiver's scores.

- `CSI_GAIN_LOCK_STRONGEST=1` forces instead the gain of the packet with the lowest `agc_gain` among packets 0-99 (the strongest sender), lowered by `CSI_GAIN_LOCK_OFFSET` AGC steps, so no sender clips; weaker senders get coarser values. At packet 100 the receiver logs `gain lock on the strongest sender: agc_force A, fft_force F (median baseline ...)`. From then on it prints the locked values without compensation, because the library compensates against its own median, which would scale the strongest link back up past 127. Power every sender before the receiver boots: a sender that starts later is not in the lock. One unusually strong packet sets the lock, so check the logged value. The library reads a forced (0, 0) as "automatic gain", so a lock that comes out at agc 0 and fft 0 leaves the AGC on. Try it on one receiver against a stock one before using it everywhere.
- `CSI_FORCE_GAIN=0` leaves the AGC running, so there is no forced gain for a reset to re-roll. The compensation multiply can then exceed 127, and `csi/parser.py` drops any line with a value outside int8, so pair it with `CSI_PRINT_RAW=1`. The host does not compensate for AGC changes yet; each C5 line carries the `agc_gain` and `fft_gain` it would need (gain change in dB is about `(agc - agc_ref) + 0.25 x (fft - fft_ref)`). Use it to record and compare, not for the demo.

## Packet budget per receiver

Each receiver prints every packet from every sender as one text line inside the Wi-Fi callback, about 0.85 KB on a C5. A 921600-baud console carries roughly 110 such lines per second, so plan for at most about 80 packets/s per receiver: the sum of the real rates of all the senders it hears. An S3 receiver prints longer lines and manages less (keep it near 55/s).

`usleep()` adds one 1 ms tick to every send period, so a sender's real rate is below its nominal one:

| `CSI_SEND_HZ` | Period | Real packets/s | Senders within ~80/s |
|---|---|---|---|
| 20 (default) | 51 ms | 19.6 | 4 (78/s) |
| 25 | 41 ms | 24.4 | 3 (73/s) |
| 40 | 26 ms | 38.5 (measured) | 2 (77/s) |
| 100 | 11 ms | 90.9 (measured) | none |

- Measure the real rate and loss from the sender's packet counter, the second column (`seq`) of every `CSI_DATA` line: a gap in it is a lost packet.
- Stay at 20 Hz or above: calibration refuses a link below 15 packets/s (`MIN_CALIBRATION_PKT_RATE_HZ` in `csi/detect.py`), and at 19.6 packets/s a link can lose only about 23%.
- For more than 4 senders, build the receivers with `CSI_PRINT_STRIDE=4` rather than lowering the rate. Lines are about 3x shorter, so the budget should grow about 3x (an estimate from line length; check the loss on the rig).

## After changing the address prefix, reflash every board

Boards on firmware from before and after the change to the `1a:0c:12:00:00` prefix do not hear each other: a new receiver ignores an old sender (`1a:00:00:00:00:xx`), and an old receiver ignores a new sender. `config/room.json` lists the new sender addresses, so the dashboard expects them.

1. Flash every sender and every receiver from the same commit, in one session.
2. Check each sender's boot line shows `mac: 1a:0c:12:00:00:<id>`, and that every receiver's `CSI_DATA` lines show `1a:0c:12:00:00:` addresses.
3. Recalibrate (flashing reset the receivers).

While boards are mixed, links go silent, and the dashboard can list a link twice: the expected one as NO_SIGNAL and an old-address one under the same label.

## Channel-11 fallback

Channel 6 is the default. If it is crowded at the venue (links lose many packets even with the senders close), move every board to channel 11. Stock esp-csi boards also default to channel 11, but the team prefix keeps their packets out.

Build a channel-11 image for every board before you need it, in directories named for it:

```
cd firmware/csi_recv
idf.py -B build_c5_ch11 -DCSI_CHANNEL=11 set-target esp32c5 && idf.py -B build_c5_ch11 build

cd ../csi_send
idf.py -B build_s3_id00_ch11 -DSENDER_ID=0x00 -DCSI_CHANNEL=11 set-target esp32s3 && idf.py -B build_s3_id00_ch11 build
idf.py -B build_c5_id01_ch11 -DSENDER_ID=0x01 -DCSI_CHANNEL=11 set-target esp32c5 && idf.py -B build_c5_id01_ch11 build
```

To switch, flash all of them (with the `esptool` command above): a board left on channel 6 is silent to the others. Check the `channel` column of the `CSI_DATA` lines reads 11 and the senders' boot lines say `wifi_channel: 11`, then recalibrate. A receiver whose links are all silent is usually on the wrong channel or still on the old prefix.

## Serial output

The receiver prints one `CSI_DATA` line per packet at 921600 baud (set in `csi_recv/sdkconfig.defaults`; `csi/parser.py` `serial_stream` opens ports at the same rate). The column layout is `C5_HEADER` in `csi/parser.py`, which the receiver also prints once at boot. If you change the printed columns, change the parser and its tests with it.

`CSI_PRINT_STRIDE` keeps the columns and prints fewer values, with `len` set to the number printed (60 at stride 4). `csi/parser.py` accepts such lines: the null-subcarrier mask is hard-coded only for full 234-value C5 lines, and for any other length the parser learns the mask from the first 20 lines. Change the stride with the dashboard stopped, and recalibrate afterwards.

## Before you flash a receiver

Flashing resets the board, and a receiver takes its gain from the first packets after boot (see Gain options), so every saved threshold on that receiver's links goes stale. Recalibrate after the last reset and read `docs/agents/hardware.md` first. A sender can be flashed without affecting receivers' gains, but expect its links to change.

- Once a receiver is calibrated, do not reset it: no flashing, replugging, `esptool`, or dashboard restart that reopens (and so may reset) its port. If one resets anyway, recalibrate its links.
- Power every sender before you reset or calibrate the receivers, so the gain is taken with all of them on the air.
- Flash only one port at a time and confirm it is the right board: list ports with `python -m serial.tools.list_ports -v`, which has no side effects. A port name can change after a reset.
- Never run `esptool` (including `chip-id`) on a receiver while the dashboard is running.
- macOS: use `/dev/cu.*` ports.

## Not in the repo

Build output (`build*/`), `sdkconfig`, and `managed_components/` are gitignored and regenerated by `idf.py`. The `esp_csi_gain_ctrl` component downloads on the first build.
