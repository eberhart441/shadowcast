#!/usr/bin/env python
"""List the connected USB serial boards, each with the room node it is (matched by USB serial = base MAC).

    python scripts/ports.py                    # room from config/room.json
    python scripts/ports.py --room other.json

Uses serial.tools.list_ports only: no port is opened, so no board resets. Exit code is 0 whatever it finds; the
flags say what does not match: a connected board that no node names, and a configured node whose board is not
connected.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serial.tools import list_ports  # noqa: E402

from csi.ports import match_ports  # noqa: E402
from csi.room import DEFAULT_ROOM_PATH, load_room  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", type=Path, default=DEFAULT_ROOM_PATH, help="room configuration (default config/room.json)")
    args = ap.parse_args(argv)
    room = load_room(args.room)
    report = match_ports(room, list_ports.comports())

    if not report.devices:
        print("no USB serial devices connected")
    else:
        width = max(len(device) for device, _, _ in report.devices)
        print(f"{'PORT':<{width}}  {'SERIAL':<17}  NODE")
        for device, serial, node in report.devices:
            label = node if node else "NO NODE (not in the room config: unplugged, or a board to add)"
            print(f"{device:<{width}}  {serial or '-':<17}  {label}")
    for name in report.absent:
        node = next(n for n in (*room.receivers, *room.senders) if n.name == name)
        print(f"NOT CONNECTED: {name} (serial {node.serial}) is configured but no board with that serial is plugged in")
    if report.unset:
        print("NO SERIAL: " + ", ".join(report.unset) + " (add \"serial\" to config/room.json to find them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
