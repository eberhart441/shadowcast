#!/usr/bin/env python
"""Set and read a node's runtime settings over its USB port, and watch its status (firmware/node).

    node.py PORT show                                    # serial, role, channel, index, nodes, round rate
    node.py PORT set index=1                             # a transceiver in slot 1 (the node saves and restarts)
    node.py PORT set role=gateway                        # the board on the dashboard host's USB
    node.py PORT set channel=11 nodes=2 round_hz=40      # kit-wide values: give every node the same ones
    node.py PORT restart
    node.py PORT status [--seconds 5]                    # status counters and log lines, as the node sends them

Every board runs the same image; what it does is these settings, kept in the node's flash. Settings: role
(transceiver or gateway), channel (2.4 GHz, 1 to 13), index (the node's slot, below nodes), nodes (members of the
fixed round, 2 to 32), round_hz (rounds per second). A transceiver that was never set only listens.

The node answers with gateway records (docs/gateway-record.md): "ok ..." or "error ...". Exit status 0 on ok, 1 on an
error reply or no reply, 2 on a usage error. The port is opened without resetting the board. Do not run this on the
gateway's port while the dashboard reads it: only one program can hold a port.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.gateway_records import COUNTER_NAMES, NodeStatus, RecordReader, Text  # noqa: E402

COMMANDS = ("show", "set", "restart", "status")
REPLY_TIMEOUT_S = 3.0


def _records(port, until: float):
    """Records read from ``port`` until the monotonic time ``until``."""
    reader = RecordReader()
    while time.monotonic() < until:
        data = port.read(port.in_waiting or 1)
        if not data:
            time.sleep(0.01)
            continue
        yield from reader.feed(data)


def describe(status: NodeStatus) -> str:
    counters = " ".join(f"{name} {status.counter(name)}" for name in COUNTER_NAMES)
    return (f"{status.serial} {status.role} channel {status.channel} up {status.uptime_us / 1e6:.1f} s "
            f"(reset reason {status.reset_reason}): {counters}")


def run(port, command: list[str], timeout_s: float = REPLY_TIMEOUT_S) -> int:
    """Run one command against an open port (anything with write, read and in_waiting). Returns the exit status."""
    until = time.monotonic() + timeout_s
    if command[0] == "status":
        for record in _records(port, until):
            if isinstance(record, NodeStatus):
                print(describe(record), flush=True)
            elif isinstance(record, Text):
                print(record.text, flush=True)
        return 0
    port.write((" ".join(command) + "\n").encode())
    for record in _records(port, until):
        if isinstance(record, Text) and record.text.startswith(("ok", "error")):
            print(record.text, flush=True)
            return 0 if record.text.startswith("ok") else 1
    print(f"node.py: no reply to {command[0]!r} within {timeout_s:g} s (is this a node running firmware/node, and "
          "is no other program holding the port?)", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="the node's serial port (python scripts/ports.py lists them by board serial)")
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("settings", nargs="*", metavar="key=value", help="for set: role, channel, index, nodes, round_hz")
    ap.add_argument("--seconds", type=float, default=5.0, help="for status: how long to listen (default 5)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "set" and not args.settings:
        ap.error("set needs key=value settings")
    if args.command != "set" and args.settings:
        ap.error(f"{args.command} takes no settings")
    from csi.parser import open_serial

    port = open_serial(args.port, timeout=0.05)
    try:
        timeout = args.seconds if args.command == "status" else REPLY_TIMEOUT_S
        return run(port, [args.command, *args.settings], timeout)
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
