# 0006. Identify boards by USB serial, not by port name

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `eb398c7` ("Find boards by USB serial: --auto-ports, scripts/ports.py, SIGTERM stops")
- **Related:** [0005](0005-vendor-the-firmware.md), [0014](0014-room-model-as-config.md)

## Context

On macOS a native-USB board's port name (`/dev/cu.usbmodem*`) follows its USB position and changes on replug and after a reset. Ports were assigned by name, and the `port_hint` recorded per node in `config/room.json` went stale. Identifying a board by asking it (`esptool chip_id`) resets it, and resetting a receiver re-baselines its gain, which makes every saved threshold on its links stale (`docs/agents/hardware.md`).

## Decision

- A board's identity is its USB serial number, which equals its base MAC. Each node in `config/room.json` carries it as `serial`.
- `--auto-ports` finds every enabled receiver's port by serial and fails, naming each receiver that is not connected.
- `scripts/ports.py` lists the connected boards with the node each one matches, and flags unmatched boards and missing nodes. It opens no port.
- `csi/ports.py` takes the result of `serial.tools.list_ports.comports()` as an argument, so it never touches hardware and tests pass fake devices.
- `--port NAME=PATH` still works for an explicit port.

## Alternatives considered

- **Port names in the config (`port_hint`).** Tried and removed: stale after every replug, and nothing read it.
- **Ask the board (`esptool chip_id`).** Rejected: it resets the board, and a reset receiver needs recalibrating.

## Consequences

- Swapping a board means updating its `serial` in the config. A node without one (today `rx3`) cannot be resolved automatically.
- Identification has no side effects, so it is safe while the dashboard runs.
- A board's serial and a sender's radio address are different numbers; the glossary separates them ("Board serial", "Sender address").
- `port_hint` lingers in `config/room_c5_only.json` and the test fixtures, where nothing reads it.

## Evidence

- **The names moved under us:** one rig update (`33dbc43`, 21:47) moved `rx2` from `usbmodem101` to `usbmodem2101`, the name `rx1` had held until then, and `rx1` to `usbmodem1101`. The same name pointed at different boards within one day.
- **The motivation, in the commit:** "Ports were assigned by USB position, so a board's /dev name changed on every replug and the port_hint in config/room.json was ignored and went stale" (`eb398c7`).
