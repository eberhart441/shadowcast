#!/usr/bin/env python
"""Update the nodes' firmware over the air: check a signed image, then roll it out in stages (docs/ota.md).

    ota.py check IMAGE                                   # would a node take it? (format, SHA-256, signature, signer)
    ota.py network                                       # the maintenance network's SSID and passphrase
    ota.py rollout IMAGE --gateway PORT [--nodes n1,n2] [--ap-channel 36] [--port 8070]
    ota.py checkin --gateway PORT [--nodes n1,n2]        # every node reports its image, nothing changes

The image is the node image signed with the kit's signing key (`espsecure sign-data --version 2`); the signer's
public key digest is in data/ota-signing.digest (`espsecure digest-sbv2-public-key`), and an image signed by any
other key, unsigned or corrupt is refused here before a node leaves the round for it.

rollout serves the image on this host (the maintenance network's access point must be up: `ota.py network` names
it; 5 GHz, or a 2.4 GHz channel 5+ away from the sensing channel), and sends one signed maintenance command per stage
through the gateway (its USB port; stop the dashboard first, only one program can hold it). Stages: 2 nodes, then
6, then the rest, 13 at most per command. A stage passes when every node reported its new image committed and was
heard on the round again; anything else stops the rollout and names the node. Nodes default to every transceiver
in the room config, in its order.

Exit status: 0 done, 1 refused or stopped, 2 usage error.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi import ota  # noqa: E402
from csi.broadcast import FRAME_ROUND, BroadcastError, decode_broadcast  # noqa: E402
from csi.gateway_records import Heard, RecordReader, Text  # noqa: E402
from csi.round_auth import KitKeyError, load_kit_key, tag_ok  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KIT_KEY = ROOT / "data" / "kit.key"
DEFAULT_SIGNER = ROOT / "data" / "ota-signing.digest"
DEFAULT_ROOM = ROOT / "config" / "room.json"
DEFAULT_PORT = 8070
DEFAULT_SENSING_CHANNEL = 6   # the node default (scripts/node.py set channel=...)
POLL_S = 0.05


def load_signer(path: Path) -> bytes:
    """The trusted signing key digest: 32 raw bytes (espsecure digest-sbv2-public-key) or 64 hex digits."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise SystemExit(f"ota.py: no signing key digest at {path}: make it with `espsecure digest-sbv2-public-key "
                         f"--keyfile data/ota-signing.pem --output {path}` (docs/ota.md)") from None
    if len(raw) == 32:
        return raw
    try:
        digest = bytes.fromhex(raw.decode().strip())
    except (UnicodeDecodeError, ValueError):
        digest = b""
    if len(digest) != 32:
        raise SystemExit(f"ota.py: {path} holds neither 32 bytes nor 64 hex digits")
    return digest


def heard_serials(records, kit_key: bytes) -> list[str]:
    """Senders of the authenticated round broadcasts among gateway records (a node is back on the round)."""
    out = []
    for record in records:
        if not isinstance(record, Heard):
            continue
        try:
            b = decode_broadcast(record.payload)
        except BroadcastError:
            continue
        if b.frame_type == FRAME_ROUND and tag_ok(record.payload, kit_key):
            out.append(b.node_id.upper())
    return out


def send_line(frame: bytes) -> bytes:
    """The gateway's USB command that puts a signed maintenance command on the sensing channel."""
    return f"ota send {frame.hex()}\n".encode()


def target_serials(room_path: Path, names: str | None) -> list[str]:
    from csi.room import load_room

    room = load_room(room_path)
    nodes = [n for n in room.transceivers if n.serial]
    if not names:
        return [n.serial.upper() for n in nodes]
    by_name = {n.name: n.serial.upper() for n in nodes}
    serials = []
    for name in names.split(","):
        name = name.strip()
        if name in by_name:
            serials.append(by_name[name])
        elif len(name.replace(":", "")) == 12:
            serials.append(name.upper())
        else:
            raise SystemExit(f"ota.py: {name!r} is neither a transceiver in {room_path} nor a board serial")
    return serials


def drive(rollout: ota.Rollout, server: ota.ImageServer, port, kit_key: bytes, clock=time.monotonic,
          sleep=time.sleep) -> str:
    """Run the rollout against the gateway on ``port`` (write, read, in_waiting) until it is done or stops."""
    reader = RecordReader()
    while True:
        now = clock()
        for report in server.take_reports():
            rollout.report(report, now)
        data = port.read(port.in_waiting or 1)
        records = reader.feed(data) if data else []
        for serial in heard_serials(records, kit_key):
            rollout.heard(serial, now)
        for r in records:
            if isinstance(r, Text) and r.text.startswith(("error", "ok ota")):
                print(f"gateway: {r.text}", flush=True)
        if rollout.step(now) != "running":
            return rollout.state
        sleep(POLL_S)


def unchecked_image(data: bytes) -> ota.ImageInfo:
    """For the rig check only: the facts of an image the host did not check, so the node's own check is tested."""
    import hashlib

    print("ota.py: --unchecked: serving an image the host did not check (rig check only)", file=sys.stderr)
    return ota.ImageInfo(size=len(data), sha256=hashlib.sha256(data).digest(), signer=b"", chip_id=0, version=None)


def cmd_check(args) -> int:
    try:
        image = ota.check_image(Path(args.image).read_bytes(), load_signer(args.signer))
    except ota.ImageError as e:
        print(f"ota.py: refused ({e.reason}): {e}", file=sys.stderr)
        return 1
    print(f"ok {args.image}: {image.size} bytes, sha256 {image.sha256.hex()}, version {image.version or '?'}, "
          f"signed by {image.signer.hex()[:16]}...")
    return 0


def cmd_network(args) -> int:
    ssid, passphrase = ota.maintenance_network(_kit_key(args))
    print(f"SSID {ssid}\npassphrase {passphrase}\nWPA2, on this host; 5 GHz, or a 2.4 GHz channel 5+ away from the "
          f"sensing channel. Nodes find it by a passive scan; give its channel to rollout with --ap-channel.")
    return 0


def _kit_key(args) -> bytes:
    try:
        return load_kit_key(args.kit_key)
    except KitKeyError as e:
        raise SystemExit(f"ota.py: {e}") from None


def cmd_rollout(args, command: int) -> int:
    kit_key = _kit_key(args)
    image, data = None, b""
    if command == ota.OTA_CMD_UPDATE:
        data = Path(args.image).read_bytes()
        try:
            image = unchecked_image(data) if args.unchecked else ota.check_image(data, load_signer(args.signer))
        except ota.ImageError as e:
            print(f"ota.py: refused ({e.reason}): {e}", file=sys.stderr)
            return 1
    serials = target_serials(args.room, args.nodes)
    if not serials:
        print(f"ota.py: no transceiver with a serial in {args.room}", file=sys.stderr)
        return 1
    sizes = tuple(int(s) for s in args.stages.split(",")) if args.stages else ota.STAGE_SIZES
    from csi.parser import open_serial

    port = open_serial(args.gateway, timeout=0.05)
    server = ota.ImageServer(data, kit_key, host=args.bind, port=args.port,
                             log=lambda m: print(m, flush=True)).start()
    try:
        rollout = ota.Rollout(ota.plan_stages(serials, sizes), kit_key, command=command, image=image,
                              port=server.port, ap_channel=args.ap_channel, sensing_channel=args.channel,
                              send=lambda frame: port.write(send_line(frame)), log=lambda m: print(m, flush=True))
        state = drive(rollout, server, port, kit_key)
    except ValueError as e:
        print(f"ota.py: {e}", file=sys.stderr)
        return 2
    finally:
        server.close()
        port.close()
    if state != "done":
        print(f"ota.py: stopped: {rollout.failure}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="would a node take this image?")
    check.add_argument("image", type=Path)
    check.add_argument("--signer", type=Path, default=DEFAULT_SIGNER, help="trusted signing key digest file")
    net = sub.add_parser("network", help="the maintenance network's SSID and passphrase")
    net.add_argument("--kit-key", type=Path, default=DEFAULT_KIT_KEY)
    for name in ("rollout", "checkin"):
        p = sub.add_parser(name, help="update the nodes in stages" if name == "rollout" else
                           "every node reports its image; nothing changes")
        if name == "rollout":
            p.add_argument("image", type=Path)
            p.add_argument("--signer", type=Path, default=DEFAULT_SIGNER, help="trusted signing key digest file")
            p.add_argument("--stages", help="first stage sizes, comma-separated (default 2,6; then 13 per stage)")
            p.add_argument("--unchecked", action="store_true",
                           help="rig check only: serve the image without the host's check, to see a node refuse it")
        p.add_argument("--gateway", required=True, help="the gateway's serial port")
        p.add_argument("--nodes", help="names in the room config or board serials, comma-separated (default: all)")
        p.add_argument("--room", type=Path, default=DEFAULT_ROOM)
        p.add_argument("--kit-key", type=Path, default=DEFAULT_KIT_KEY)
        p.add_argument("--ap-channel", type=int, default=0,
                       help="the maintenance network's channel (default 0: the nodes scan passively for it)")
        p.add_argument("--channel", type=int, default=DEFAULT_SENSING_CHANNEL, help="the sensing channel (default 6)")
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"image server port (default {DEFAULT_PORT})")
        p.add_argument("--bind", default="0.0.0.0", help="image server address (default: every interface)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "network":
        return cmd_network(args)
    if not ota.channels_clear(args.channel, args.ap_channel):
        print(f"ota.py: a maintenance network on channel {args.ap_channel} overlaps sensing channel {args.channel}",
              file=sys.stderr)
        return 2
    return cmd_rollout(args, ota.OTA_CMD_UPDATE if args.command == "rollout" else ota.OTA_CMD_REPORT)


if __name__ == "__main__":
    sys.exit(main())
