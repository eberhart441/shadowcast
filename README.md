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
installed and exported (`. $IDF_PATH/export.sh`).

```bash
git clone https://github.com/espressif/esp-csi.git
cd esp-csi/examples/get-started
```

Both nodes must use the **same WiFi channel**. Pick one (e.g. 6) and keep it
fixed. Use the router channel only if you are connecting through a router;
the simplest setup is ESP-NOW with no router.

### Sender node (`csi_send`)

```bash
cd csi_send
idf.py set-target esp32          # or esp32s3 / esp32c3 / esp32c6
idf.py menuconfig                # Example Configuration -> set channel (e.g. 6)
idf.py -p <PORT> flash
```

The sender transmits at 100 Hz by default (`CONFIG_SEND_FREQUENCY`; confirm in
menuconfig, and in `app_main.c` if your esp-csi version hardcodes it).

### Receiver node (`csi_recv`)

```bash
cd ../csi_recv
idf.py set-target esp32
idf.py menuconfig                # same channel as the sender
idf.py -p <PORT> flash
idf.py -p <PORT> monitor         # confirm lines starting with CSI_DATA
```

(Close the monitor before running the Python scripts; only one program can
hold the port. Exit monitor with `Ctrl+]`.)

Notes:
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

## Layout

- `csi/` library: parser, simulator, detectors
- `scripts/` CLIs: live viewer, recorder, live detection, offline eval
- `data/` recordings (gitignored)
- `tests/` pytest suite
