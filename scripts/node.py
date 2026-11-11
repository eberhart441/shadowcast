#!/usr/bin/env python
"""Set and read a node's runtime settings over its USB port, and watch its status (firmware/node).

    node.py PORT show                                    # serial, role, channel, round rate, slot guard
    node.py PORT set role=gateway                        # the board on the dashboard host's USB (saves, restarts)
    node.py PORT set channel=11 round_hz=40 guard_us=250 # kit-wide values: give every node the same ones
    node.py PORT restart
    node.py PORT status [--seconds 5]                    # status counters and log lines, as the node sends them
    node.py keygen [data/kit.key]                        # make the kit key, once per kit (no port)
    node.py PORT key data/kit.key                        # give a node the kit key (it saves and restarts)
    node.py PORT key                                     # the node's kit key check value, or "none"

Every board runs the same image; what it does is these settings, kept in the node's flash. Settings: role
(transceiver or gateway), channel (2.4 GHz, 1 to 13), round_hz (rounds per second), guard_us (the slot guard, 50 to
5000 us). A transceiver needs none of them: with the defaults it joins the round on its own (the round scheduler
gives it a node index and a slot; there is no index or member count to set).

The kit key authenticates round broadcasts (docs/round-broadcast.md "Kit key"): every node of a kit and the dashboard
host share it. keygen writes it to a file (default data/kit.key, which is not in git) and never overwrites one. key
sends it to the node, which keeps it in flash and answers with its check value; node.py exits 1 unless that is the
file's check value. Neither the node nor this script ever prints the key itself. A transceiver without a kit key
only listens.

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
from csi.round_auth import KitKeyError, kit_key_check, load_kit_key, write_kit_key  # noqa: E402

COMMANDS = ("show", "set", "restart", "status", "key")
DEFAULT_KIT_KEY = Path(__file__).resolve().parents[1] / "data" / "kit.key"
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
    reply = ask(port, command, until)
    return 1 if reply is None or not reply.startswith("ok") else 0


def ask(port, command: list[str], until: float, show: str = "") -> str | None:
    """Send one command line; print and return the node's "ok"/"error" reply (None, and a message, if none came by
    the monotonic time ``until``). ``show`` names the command in that message instead of its first word."""
    port.write((" ".join(command) + "\n").encode())
    for record in _records(port, until):
        if isinstance(record, Text) and record.text.startswith(("ok", "error")):
            print(record.text, flush=True)
            return record.text
    print(f"node.py: no reply to {show or command[0]!r} within the timeout (is this a node running firmware/node, "
          "and is no other program holding the port?)", file=sys.stderr)
    return None


def provision(port, key_file, timeout_s: float = REPLY_TIMEOUT_S) -> int:
    """Give the node on ``port`` the kit key in ``key_file``; 0 only if it answers with that key's check value."""
    try:
        key = load_kit_key(key_file)
    except KitKeyError as e:
        print(f"node.py: {e}", file=sys.stderr)
        return 1
    reply = ask(port, ["key", key.hex()], time.monotonic() + timeout_s, show="key")
    if reply is None or not reply.startswith("ok"):
        return 1
    want = f"check={kit_key_check(key)}"
    if want not in reply:
        print(f"node.py: the node did not take the key: its check is not {want[6:]} (the key file's)", file=sys.stderr)
        return 1
    return 0


def keygen(path: Path) -> int:
    try:
        key = write_kit_key(path)
    except KitKeyError as e:
        print(f"node.py: {e}", file=sys.stderr)
        return 1
    print(f"wrote a new kit key to {path}, check {kit_key_check(key)}. Give it to every node with "
          f"`python scripts/node.py PORT key {path}`; the dashboard reads it with --kit-key (default data/kit.key).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="the node's serial port (python scripts/ports.py lists them by board serial)")
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("settings", nargs="*", metavar="key=value",
                    help="for set: role, channel, round_hz, guard_us; for key: the kit key file")
    ap.add_argument("--seconds", type=float, default=5.0, help="for status: how long to listen (default 5)")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["keygen"]:
        if len(argv) > 2:
            build_parser().error("keygen takes one file name at most")
        return keygen(Path(argv[1]) if len(argv) == 2 else DEFAULT_KIT_KEY)
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "set" and not args.settings:
        ap.error("set needs key=value settings")
    if args.command == "key" and len(args.settings) > 1:
        ap.error("key takes one kit key file at most")
    if args.command not in ("set", "key") and args.settings:
        ap.error(f"{args.command} takes no settings")
    from csi.parser import open_serial

    port = open_serial(args.port, timeout=0.05)
    try:
        timeout = args.seconds if args.command == "status" else REPLY_TIMEOUT_S
        if args.command == "key" and args.settings:
            return provision(port, args.settings[0], timeout)
        return run(port, [args.command, *args.settings], timeout)
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
