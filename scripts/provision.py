#!/usr/bin/env python
"""Flash and set up new boards one after another: plug a board in, it is flashed, keyed, numbered and logged.

    provision.py                    # every new board becomes a transceiver; runs until Ctrl+C
    provision.py --role gateway --once
    provision.py --reflash          # also redo boards already in the inventory (they keep their number)
    provision.py --id 3             # one board, logged under the ID already written on it

For each Espressif USB board that appears and is not yet in the inventory (data/boards.csv) it:

1. flashes the signed node image over USB (bootloader, partition table, OTA data, firmware/node/build/node-signed.bin;
   build and sign it first, docs/ota.md "Once per board");
2. waits for the board to come back (its port name can change), then gives a transceiver the kit key
   (scripts/node.py PORT key data/kit.key), or sets a gateway's role (scripts/node.py PORT set role=gateway);
3. reads it back with scripts/node.py PORT show;
4. gives it the next board number, appends it to data/boards.csv, and prints the number to write on its label.

Boards are told apart by USB serial (the base MAC), never by port name. Any esptool command resets a board, so do not
run this while a dashboard reads a gateway on this machine. Exit status: 0 done (Ctrl+C or --once), 1 a board failed
with --once, 2 a missing file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serial.tools import list_ports  # noqa: E402

from csi.ports import normalise_serial  # noqa: E402

ESPRESSIF_VID = 0x303A
BUILD = ROOT / "firmware" / "node" / "build"
IMAGE = BUILD / "node-signed.bin"
FLASH_FILES = (("0x2000", BUILD / "bootloader" / "bootloader.bin"),
               ("0x8000", BUILD / "partition_table" / "partition-table.bin"),
               ("0xf000", BUILD / "ota_data_initial.bin"),
               ("0x20000", IMAGE))
KIT_KEY = ROOT / "data" / "kit.key"
INVENTORY = ROOT / "data" / "boards.csv"
FIELDS = ("number", "serial", "role", "provisioned_at", "image_sha256", "key_check", "show")
NODE = [sys.executable, str(ROOT / "scripts" / "node.py")]


def connected() -> dict[str, tuple[str, str]]:
    """{normalised serial: (port, serial as reported)} for every Espressif USB board plugged in."""
    out = {}
    for info in list_ports.comports():
        if info.vid == ESPRESSIF_VID and info.serial_number:
            out[normalise_serial(info.serial_number)] = (info.device, info.serial_number)
    return out


def wait_for(key: str, timeout_s: float, settle_s: float = 1.5) -> str | None:
    """The port of the board with serial ``key`` once it has been listed for ``settle_s``; None after the timeout."""
    end = time.monotonic() + timeout_s
    seen_since = None
    while time.monotonic() < end:
        port = connected().get(key, (None, ""))[0]
        if port is None:
            seen_since = None
        elif seen_since is None:
            seen_since = time.monotonic()
        elif time.monotonic() - seen_since >= settle_s:
            return port
        time.sleep(0.25)
    return None


def load_inventory() -> list[dict[str, str]]:
    if not INVENTORY.exists():
        return []
    with INVENTORY.open(newline="") as f:
        return list(csv.DictReader(f))


def save_inventory(rows: list[dict[str, str]]) -> None:
    INVENTORY.parent.mkdir(exist_ok=True)
    with INVENTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def run(cmd: list[str], what: str) -> str | None:
    """Run ``cmd``, echo its output; its stdout on success, None (after saying what failed) otherwise."""
    print("  +", " ".join(cmd[1:] if cmd[0] == sys.executable else cmd), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        print(f"  FAILED: {what} (exit {p.returncode})\n" + "\n".join("    " + line for line in out.splitlines()[-15:]))
        return None
    return p.stdout


def node_cmd(key: str, args: list[str], what: str, tries: int = 3) -> str | None:
    """Run scripts/node.py against the board, re-finding its port each try (it restarts after saving settings)."""
    for attempt in range(tries):
        port = wait_for(key, timeout_s=15)
        if port is None:
            print(f"  FAILED: {what}: the board did not come back on USB within 15 s")
            return None
        out = run([*NODE, port, *args], what if attempt == tries - 1 else f"{what} (will retry)")
        if out is not None:
            return out
        time.sleep(2)
    return None


def provision(key: str, serial: str, port: str, role: str, number: int, image_sha: str) -> dict[str, str] | None:
    print(f"\n=== board #{number}: serial {serial} on {port} -> {role}", flush=True)
    # A board that has just been plugged in is listed before Windows has finished setting up its port.
    port = wait_for(key, timeout_s=20, settle_s=3.0)
    if port is None:
        print("  FAILED: the board's port did not stay listed for 3 s")
        return None
    flash = [sys.executable, "-m", "esptool", "--chip", "esp32c5", "-p", port, "-b", "460800",
             "--before", "default-reset", "--after", "hard-reset", "write-flash", "--flash-mode", "dio",
             "--flash-freq", "80m", "--flash-size", "keep"]
    for addr, path in FLASH_FILES:
        flash += [addr, str(path)]
    print("  +", " ".join(flash[1:]), flush=True)
    p = subprocess.run(flash, capture_output=True, text=True)
    out = p.stdout + p.stderr
    # On the C5's built-in USB, esptool can fail with "Serial data stream stopped" *after* it has written and
    # verified every file, at the reset at the end. That is a good flash; the readback below proves the boot.
    reset_only = (out.count("Wrote ") == len(FLASH_FILES) and "Hard resetting" in out
                  and out.rfind("ERROR") > out.rfind("Hard resetting"))
    if p.returncode != 0 and not reset_only:
        print(f"  FAILED: flash (exit {p.returncode})\n" + "\n".join("    " + line for line in out.splitlines()[-15:]))
        return None
    if reset_only:
        print("  flashed and verified; esptool lost the port at the final reset (normal on this board)")
    if role == "gateway":
        reply = node_cmd(key, ["set", "role=gateway"], "set role=gateway")
        key_check = ""
    else:
        # The role is kept in NVS across flashes, so a board that was a gateway stays one unless told otherwise.
        reply = node_cmd(key, ["set", "role=transceiver"], "set role=transceiver")
        if reply is None:
            return None
        time.sleep(1.0)  # it saves and restarts
        reply = node_cmd(key, ["key", str(KIT_KEY)], "give the kit key")
        key_check = reply.split("check=")[1].split()[0] if reply and "check=" in reply else ""
    if reply is None:
        return None
    time.sleep(1.0)  # it saves and restarts
    show = node_cmd(key, ["show"], "read the settings back")
    if show is None:
        return None
    show = " ".join(show.split())
    if f"role={role}" not in show:
        print(f"  FAILED: the board does not report role={role}: {show}")
        return None
    # "configured=no" only means no settings were stored with `node.py set`; a transceiver runs on the defaults.
    # What matters is that it holds the kit key and its signing passed the self-test, which `key` reports.
    if role == "transceiver":
        keyed = node_cmd(key, ["key"], "read the kit key status back")
        if keyed is None or f"check={key_check}" not in keyed or "FAILED" in keyed:
            print(f"  FAILED: the board does not report kit key check {key_check}: {keyed!r}")
            return None
        show = f"{show} | {' '.join(keyed.split())}"
    return {"number": str(number), "serial": serial, "role": role,
            "provisioned_at": dt.datetime.now().isoformat(timespec="seconds"), "image_sha256": image_sha,
            "key_check": key_check, "show": show}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=("transceiver", "gateway"), default="transceiver")
    ap.add_argument("--once", action="store_true", help="stop after one board")
    ap.add_argument("--reflash", action="store_true", help="also redo boards already in the inventory")
    ap.add_argument("--id", type=int, metavar="N",
                    help="the ID already written on the board; implies --once (default: next free ID from 0)")
    args = ap.parse_args(argv)
    if args.id is not None:
        args.once = True

    missing = [p for _, p in FLASH_FILES if not p.exists()] + ([KIT_KEY] if args.role == "transceiver"
                                                               and not KIT_KEY.exists() else [])
    if missing:
        print("provision.py: missing " + ", ".join(str(p) for p in missing) + " (docs/ota.md, node.py keygen)",
              file=sys.stderr)
        return 2
    image_sha = hashlib.sha256(IMAGE.read_bytes()).hexdigest()

    rows = load_inventory()
    done_now: set[str] = set()   # boards finished in this run: never redone until they are unplugged
    failed: set[str] = set()     # boards that failed in this run: retried only after a replug
    print(f"image {IMAGE.name} sha256 {image_sha[:16]}..., inventory {INVENTORY} ({len(rows)} boards)")
    print("Plug in boards (one at a time, or several on a powered hub). Ctrl+C to stop.", flush=True)
    try:
        while True:
            present = connected()
            done_now &= present.keys()
            failed &= present.keys()
            known = {normalise_serial(r["serial"]): r for r in rows}
            todo = [(k, v) for k, v in present.items() if k not in done_now and k not in failed
                    and (args.reflash or k not in known)]
            if args.id is not None:   # leave other logged boards alone (the gateway stays plugged in)
                todo = [(k, v) for k, v in todo if k not in known or known[k]["number"] == str(args.id)]
            if not todo:
                time.sleep(0.5)
                continue
            key, (port, serial) = todo[0]
            old = known.get(key)
            if args.id is not None:
                clash = [r for r in rows if r["number"] == str(args.id) and normalise_serial(r["serial"]) != key]
                if clash:
                    print(f"provision.py: ID {args.id} is already board {clash[0]['serial']} in {INVENTORY}",
                          file=sys.stderr)
                    return 1
                number = args.id
            elif old:
                number = int(old["number"])
            else:
                number = max((int(r["number"]) for r in rows), default=-1) + 1
            row = provision(key, serial, port, args.role, number, image_sha)
            if row is None:
                failed.add(key)
                print(f"*** board {serial} FAILED: not numbered. Unplug and replug it to retry.", flush=True)
                if args.once:
                    return 1
                continue
            rows = [r for r in rows if normalise_serial(r["serial"]) != key] + [row]
            rows.sort(key=lambda r: int(r["number"]))
            save_inventory(rows)
            done_now.add(key)
            print(f"  {row['show']}")
            label = "" if args.id is not None else f" Write #{number} on it."
            print(f"\n*** DONE: board #{number} = serial {serial} ({args.role}).{label} Unplug it.\n", flush=True)
            if args.once:
                return 0
    except KeyboardInterrupt:
        print(f"\nstopped; {len(rows)} boards in {INVENTORY}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
