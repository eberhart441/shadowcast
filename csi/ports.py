"""Find each board by its USB serial (its base MAC) instead of its /dev name, which changes on every replug.

The functions take the result of ``serial.tools.list_ports.comports()`` as an argument (objects with ``device``,
``serial_number`` and ``vid``), so they never touch the hardware and tests can pass fake devices. Nothing here opens
a port.

    resolve_receiver_ports(room, listed) -> {receiver name: device path}   raises PortResolutionError
    match_ports(room, listed)            -> PortReport (for scripts/ports.py)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .room import Node, Room


class PortResolutionError(Exception):
    """An enabled receiver's board is not connected, or its port cannot be told apart."""


@dataclass(frozen=True)
class PortReport:
    devices: tuple[tuple[str, str, str], ...]  # (device, serial, node name or "" when none matches)
    absent: tuple[str, ...]                    # nodes with a serial whose board is not connected
    unset: tuple[str, ...]                     # nodes with no serial in the config


def normalise_serial(value: str) -> str:
    """Upper-case hex digits only: ``38:44:be:a5`` and ``3844bea5`` are the same board."""
    return re.sub(r"[^0-9A-F]", "", (value or "").upper())


def _devices_by_serial(listed: Iterable) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for info in listed:
        key = normalise_serial(getattr(info, "serial_number", None) or "")
        if key:
            out.setdefault(key, []).append(info.device)
    return out


def resolve_receiver_ports(room: Room, listed: Iterable) -> dict[str, str]:
    """The port of every enabled receiver, found by its serial. Raises PortResolutionError naming each problem."""
    devices = _devices_by_serial(listed)
    ports: dict[str, str] = {}
    problems: list[str] = []
    for rx in room.receivers:
        key = normalise_serial(rx.serial)
        if not key:
            problems.append(f"{rx.name} (no serial in config/room.json)")
        elif key not in devices:
            problems.append(f"{rx.name} (serial {rx.serial} not connected)")
        elif len(devices[key]) > 1:
            problems.append(f"{rx.name} (serial {rx.serial} matches {len(devices[key])} ports: "
                            f"{', '.join(devices[key])})")
        else:
            ports[rx.name] = devices[key][0]
    if problems:
        raise PortResolutionError("--auto-ports cannot find: " + "; ".join(problems)
                                  + ". Run scripts/ports.py to see connected boards.")
    return ports


def match_ports(room: Room, listed: Iterable) -> PortReport:
    """Every connected USB serial device with the node it matches, and the nodes that match no device."""
    nodes: tuple[Node, ...] = (*room.receivers, *room.senders)
    by_key = {normalise_serial(n.serial): n.name for n in nodes if normalise_serial(n.serial)}
    devices, seen = [], set()
    for info in listed:
        if getattr(info, "vid", None) is None:  # not a USB device (Bluetooth and the like)
            continue
        serial = getattr(info, "serial_number", None) or ""
        key = normalise_serial(serial)
        seen.add(key)
        devices.append((info.device, serial, by_key.get(key, "")))
    absent = tuple(n.name for n in nodes if normalise_serial(n.serial) and normalise_serial(n.serial) not in seen)
    unset = tuple(n.name for n in nodes if not normalise_serial(n.serial))
    return PortReport(devices=tuple(devices), absent=absent, unset=unset)
