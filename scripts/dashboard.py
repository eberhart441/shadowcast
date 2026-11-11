#!/usr/bin/env python
"""Dashboard server: the multi-link pipeline plus a small HTTP interface (JSON state, SSE events, a page).

    dashboard.py --simulate-room                                   # a simulated person walks the room, no hardware
    dashboard.py --simulate --room config/room_tr8.json            # simulated transceiver rig: round broadcasts
    dashboard.py --node-gateway COM7 --room config/room_tr2.json   # real transceivers, heard by a gateway node
    dashboard.py --simulate --room config/room_tr8.json --record run.rounds --label empty   # record it, labelled
    dashboard.py --replay-rounds run.rounds --speed 8              # replay a round recording through the pipeline
    dashboard.py --simulate-room --http-port 8000 --host 0.0.0.0   # reachable from the network (opt-in)
    dashboard.py --port rx1=/dev/cu.usbmodem2101 --port rx2=/dev/cu.usbmodem101   # fixed: links UNCALIBRATED until set
    dashboard.py --auto-ports                                      # each receiver's port found by its serial (scripts/ports.py)
    dashboard.py --port rx1=/dev/cu.usbmodem2101 --thresholds data/links_cal1_m1.json   # fixed thresholds
    dashboard.py --port ... --auto-threshold                       # experimental auto mode, no calibration needed
    dashboard.py --simulate-room --log run.jsonl                   # also append the state stream to a file
    dashboard.py --replay run.jsonl --speed 8                      # serve a logged run (1 = original pace, max = fastest)
    dashboard.py --replay-session data/hw5 --speed 8 [--margin X | --thresholds links.json]
    dashboard.py --auto-ports --page web/ui-overhaul/index.html   # any source: serve the new UI at / instead

Endpoints: GET /  (page), GET /api/state  (one payload), GET /api/events  (SSE, ~5 Hz).
Binds to 127.0.0.1 unless --host says otherwise. Mode: links are fixed by default (--thresholds file, or the
simulated empty-room calibration with --simulate-room; without either, links show UNCALIBRATED until calibrated
or set). --auto-threshold switches live sources to auto mode (experimental: each link learns its own baseline,
see csi/adaptive.py) and ignores any thresholds file. A Set on a link makes it fixed.
Ctrl+C (or SIGTERM) stops the server and releases every port.
A replay needs no serial port; when it has finished, the final state stays up (status.finished is true).
--simulate runs the transceiver pipeline: the room's transceivers send round broadcasts (docs/round-broadcast.md)
and every link is judged by its own adaptive baseline (WARMUP for its first 10 s or so). --node-gateway runs the same
pipeline on real transceivers (firmware/node), from the binary records of a gateway node (docs/gateway-record.md).
--record writes every round broadcast and the labels to a round recording (docs/round-recording.md); while it
runs, type a label (and optional key=value extras, e.g. "walking mark=A3") and Enter to switch label.
In the transceiver pipeline a zone that stops moving reads POSSIBLY_OCCUPIED for --possibly-occupied-s (default 60 s)
before EMPTY: a person who stands still fades from the score (csi/room_state.py).
"""
from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import margin_arg  # noqa: E402
from csi.detect import DEFAULT_MARGIN  # noqa: E402
from csi.links import load_thresholds  # noqa: E402
from csi.multisource import parse_port_specs  # noqa: E402
from csi.gateway import gateway_streams  # noqa: E402
from csi.gateway_records import gateway_source  # noqa: E402
from csi.rawlog import tee_sources  # noqa: E402
from csi.ports import PortResolutionError, resolve_receiver_ports  # noqa: E402
from csi.parser import Stream, serial_stream  # noqa: E402
from csi.recording import LABELS, validate_label  # noqa: E402
from csi.replay import ReplayError, ReplayServer, load_log, rounds_dashboard, session_dashboard  # noqa: E402
from csi.room_state import POSSIBLY_OCCUPIED_S  # noqa: E402
from csi.room import DEFAULT_ROOM_PATH, Room, load_room  # noqa: E402
from csi.round_auth import TEST_KIT_KEY, KitKeyError, kit_key_check, load_kit_key  # noqa: E402
from csi.round_recording import RoundRecorder  # noqa: E402
from csi.server import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_THRESHOLDS_FILE, DashboardServer, FrameServer, \
    controls_allowed, \
    default_walk, looping_room_streams, simulated_rig_streams, simulated_thresholds  # noqa: E402

TICK_S = 0.5
DEFAULT_KIT_KEY_PATH = Path(__file__).resolve().parents[1] / "data" / "kit.key"   # data/ is not in git


def speed_arg(text: str) -> float:
    """--speed: a positive factor (1 is the original pace), or 'max' for as fast as possible."""
    if text == "max":
        return math.inf
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not value > 0 or math.isinf(value):
        raise argparse.ArgumentTypeError(f"speed must be a positive number or 'max', got {text!r}")
    return value


def quiet_seconds(text: str) -> float:
    """--sim-quiet-first, --possibly-occupied-s: a non-negative number of seconds."""
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0 or math.isinf(value):
        raise argparse.ArgumentTypeError(f"seconds must be a number >= 0, got {text!r}")
    return value


def label_arg(text: str) -> str:
    """--label: one of the recording labels."""
    try:
        return validate_label(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate-room", action="store_true",
                   help="a simulated person walks a loop through the room in real time (no hardware)")
    g.add_argument("--simulate", action="store_true",
                   help="a simulated transceiver rig: the room's transceivers send round broadcasts while a person "
                        "walks a loop (no hardware; every link in auto mode). Needs a room with transceivers, "
                        "for example --room config/room_tr8.json")
    g.add_argument("--port", action="append", metavar="[NAME=]PATH",
                   help="serial port of a csi_recv node; repeat for each receiver. Unnamed ports get rx1, rx2...")
    g.add_argument("--auto-ports", action="store_true",
                   help="find each enabled receiver's port by its USB serial (config/room.json \"serial\"); fails "
                        "naming any receiver whose board is not connected. Same as --port for the rest")
    g.add_argument("--gateway", metavar="PATH",
                   help="serial port of the gateway board for wireless receivers (csi_recv built with CSI_GATEWAY=1); "
                        "receivers are told apart by their \"serial\" in config/room.json")
    g.add_argument("--node-gateway", metavar="PATH",
                   help="serial port of a gateway node of the transceiver rig (firmware/node, role gateway): it hands "
                        "on every round broadcast it hears (every link in auto mode). Needs a room with transceivers "
                        "whose \"serial\" is each board's, for example --room config/room_tr2.json")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--seed", type=int, default=0, help="simulator seed")
    g.add_argument("--sim-quiet-first", type=quiet_seconds, default=0.0, metavar="SECONDS",
                   help="with --simulate-room or --simulate: the room is empty for SECONDS before the walk starts, "
                        "so Calibrate can run in the demo (the default calibration takes about 45 s; default 0)")
    r = ap.add_argument_group("replay (no hardware)")
    r.add_argument("--replay", type=Path, metavar="run.jsonl",
                   help="serve a run written with --log: the same states, at the logged pace")
    r.add_argument("--replay-session", type=Path, metavar="DIR",
                   help="feed a recorded session (scripts/record.py) through the live pipeline, paced by its "
                        "recorded host times; with one-link recordings the heatmap has one link")
    r.add_argument("--replay-rounds", type=Path, metavar="run.rounds",
                   help="feed a round recording (--record) through the live transceiver pipeline, paced by its "
                        "recorded host times, every link in auto mode; the room is the one recorded with the run")
    r.add_argument("--speed", type=speed_arg, default=None, metavar="1|N|max",
                   help="replay speed: 1 is the original pace, N is N times faster, max as fast as possible "
                        "(default 1)")
    r.add_argument("--margin", type=margin_arg, default=None, metavar="X",
                   help=f"with --replay-session and no --thresholds: calibration margin, threshold = X x p99 of "
                        f"the session's empty recording (default {DEFAULT_MARGIN:g})")
    ap.add_argument("--raw-log", type=Path, default=None, metavar="raw.tsv",
                    help="also append every CSI line the receivers deliver, with its host time, to this file "
                         "(host_ts, receiver, line; for exact offline replays: scripts/accuracy_eval.py)")
    ap.add_argument("--record", type=Path, default=None, metavar="run.rounds",
                    help="with --simulate or --node-gateway: also write every round broadcast, with its host time and source, and the "
                         "labels to this round recording (replay it with --replay-rounds)")
    ap.add_argument("--label", type=label_arg, default=None, metavar="LABEL",
                    help=f"with --record: the label from the start ({', '.join(LABELS)}); type another label and "
                         "Enter while it runs to switch")
    ap.add_argument("--log", type=Path, metavar="run.jsonl",
                    help="append the state stream to this file: a header line (room, thresholds, start time) and "
                         "then one payload per line, the same as /api/events")
    ap.add_argument("--kit-key", type=Path, default=None, metavar="PATH",
                    help="the kit key file the round broadcasts are verified with (scripts/node.py keygen); "
                         "--node-gateway defaults to data/kit.key, --replay-rounds to the simulator's test key")
    ap.add_argument("--possibly-occupied-s", type=quiet_seconds, default=None, metavar="SECONDS",
                    help="transceiver pipeline (--simulate, --node-gateway, --replay-rounds): how long a zone that "
                         f"stops moving reads POSSIBLY_OCCUPIED before EMPTY (default {POSSIBLY_OCCUPIED_S:g}; 0 "
                         "turns the hold off)")
    ap.add_argument("--room", type=Path, default=DEFAULT_ROOM_PATH, help="room configuration (default config/room.json)")
    ap.add_argument("--thresholds", metavar="links.json",
                    help="per-link fixed thresholds from links_live.py --calibrate. With --simulate-room the default is "
                         "a calibration on a simulated empty room; with --replay-session the default is one "
                         "calibrated on the session's first empty recording.")
    ap.add_argument("--auto-threshold", action="store_true",
                    help="experimental auto mode: each link adapts to its own noise baseline (about 10 s of quiet at "
                         "start; a person still for about 25 s is absorbed; walking is detected less sensitively, since "
                         "the room needs two links to agree). Ignores --thresholds. Live sources only")
    s = ap.add_argument_group("server")
    s.add_argument("--http-port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    s.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST}, localhost only; 0.0.0.0 for the network)")
    s.add_argument("--allow-control", action="store_true",
                   help="allow calibrate, threshold, save and parameter commands when --host is not localhost "
                        "(there is no authentication: only on a network you trust)")
    s.add_argument("--save-thresholds", type=Path, default=None, metavar="PATH",
                   help=f"where POST /api/thresholds/save writes (default: --thresholds, else "
                        f"{DEFAULT_THRESHOLDS_FILE} in the working directory)")
    s.add_argument("--page", type=Path, default=None, metavar="PATH",
                   help="the HTML page served at / (default web/index.html); "
                        "--page web/ui-overhaul/index.html serves the new UI")
    return ap


def check_source(args: argparse.Namespace) -> None:
    chosen = [flag for flag, on in (("--simulate-room", args.simulate_room), ("--simulate", args.simulate),
                                    ("--port", bool(args.port)),
                                    ("--auto-ports", args.auto_ports),
                                    ("--gateway", args.gateway is not None),
                                    ("--node-gateway", args.node_gateway is not None),
                                    ("--replay", args.replay is not None),
                                    ("--replay-session", args.replay_session is not None),
                                    ("--replay-rounds", args.replay_rounds is not None)) if on]
    if len(chosen) > 1:
        raise SystemExit(f"error: use one source, not {' and '.join(chosen)}")
    if not chosen:
        raise SystemExit("error: pass a source: --simulate-room, --simulate, --port [NAME=]PATH (repeat for each receiver), "
                         "--auto-ports, --gateway PATH, --node-gateway PATH, --replay run.jsonl, --replay-session DIR or "
                         "--replay-rounds run.rounds")
    replaying = args.replay is not None or args.replay_session is not None or args.replay_rounds is not None
    if args.speed is not None and not replaying:
        raise SystemExit("error: --speed applies to --replay and --replay-session only")
    if args.sim_quiet_first and not (args.simulate_room or args.simulate):
        raise SystemExit("error: --sim-quiet-first applies to --simulate-room and --simulate only")
    rounds = "--simulate" if args.simulate else "--node-gateway" if args.node_gateway is not None else None
    if rounds and args.thresholds is not None:
        raise SystemExit(f"error: --thresholds does not apply to {rounds}: every transceiver link is judged by its "
                         "own adaptive baseline")
    if rounds and args.raw_log is not None:
        raise SystemExit(f"error: --raw-log records CSI lines; {rounds} carries round broadcasts, which it cannot "
                         "record (--record does)")
    if args.kit_key is not None and not (rounds == "--node-gateway" or args.replay_rounds is not None):
        raise SystemExit("error: --kit-key applies to --node-gateway and --replay-rounds only (--simulate signs with "
                         "the published test key)")
    if args.possibly_occupied_s is not None and not (rounds or args.replay_rounds is not None):
        raise SystemExit("error: --possibly-occupied-s applies to --simulate, --node-gateway and --replay-rounds only "
                         "(the room state of transceiver links)")
    if args.replay_rounds is not None and args.thresholds is not None:
        raise SystemExit("error: --thresholds does not apply to --replay-rounds: every transceiver link is judged by "
                         "its own adaptive baseline")
    if args.replay_rounds is not None and args.raw_log is not None:
        raise SystemExit("error: --raw-log records CSI lines; a round recording holds round broadcasts")
    if args.record is not None and not rounds:
        raise SystemExit("error: --record writes round broadcasts and applies to --simulate and --node-gateway only "
                         "(--raw-log records CSI lines from receivers)")
    if args.label is not None and args.record is None:
        raise SystemExit("error: --label needs --record")
    if args.margin is not None and args.replay_session is None:
        raise SystemExit("error: --margin applies to --replay-session only")
    if args.replay is not None and args.log is not None:
        raise SystemExit("error: --log records a live run; it cannot be combined with --replay")
    if args.replay is not None and args.thresholds is not None:
        raise SystemExit("error: --thresholds does not apply to --replay: a log already holds its states")
    if args.auto_threshold and replaying:
        raise SystemExit("error: --auto-threshold applies to live sources (--port, --simulate-room) only")


def check_page(args: argparse.Namespace) -> None:
    """--page must name a file: a wrong path would serve the server's "web/index.html is missing" stub."""
    if args.page is not None and not args.page.is_file():
        raise SystemExit(f"error: --page {args.page} is not a file")


def resolve_auto_ports(room: Room) -> list[str]:
    """--auto-ports: ``NAME=PATH`` for each enabled receiver, its port found by serial. Raises SystemExit if any is missing."""
    from serial.tools import list_ports  # lazy, as serial_stream: --simulate-room needs no pyserial port

    try:
        ports = resolve_receiver_ports(room, list_ports.comports())
    except PortResolutionError as e:
        raise SystemExit(f"error: {e}")
    return [f"{name}={path}" for name, path in ports.items()]


def _stop_on_sigterm(signum, frame) -> None:
    raise KeyboardInterrupt  # SIGTERM stops the server exactly as Ctrl+C does


def load_link_thresholds(path: Optional[Path]) -> Optional[Mapping[str, float]]:
    if path is None:
        return None
    try:
        return load_thresholds(path)
    except (OSError, ValueError) as e:
        raise SystemExit(f"error: cannot read thresholds {path}: {e}")


def server_mode(args: argparse.Namespace) -> str:
    """"fixed" by default; "auto" only with --auto-threshold (live sources).

    Replays are fixed (from the log, or a calibration on the session's first empty recording), except a round
    recording: the transceiver pipeline is always auto, as --simulate.
    """
    if args.replay_rounds is not None:
        return "auto"
    if args.replay is not None or args.replay_session is not None:
        return "fixed"
    return "auto" if args.auto_threshold or args.simulate or args.node_gateway is not None else "fixed"


def hold_s(args: argparse.Namespace) -> float:
    """The possibly-occupied hold: --possibly-occupied-s, else the default."""
    return POSSIBLY_OCCUPIED_S if args.possibly_occupied_s is None else args.possibly_occupied_s


def kit_key(args: argparse.Namespace) -> bytes:
    """The kit key the round broadcasts are verified with: --kit-key, else data/kit.key for the real rig (which
    must exist) and the published test key for a round replay. Raises SystemExit for a missing or bad key file."""
    if args.kit_key is None and args.node_gateway is None:
        return TEST_KIT_KEY
    try:
        return load_kit_key(args.kit_key or DEFAULT_KIT_KEY_PATH)
    except KitKeyError as e:
        raise SystemExit(f"error: {e}")


def record_rounds(args: argparse.Namespace, room: Room, streams: dict, notes: str) -> dict:
    """With --record: tee the round broadcast streams into a round recording (args.recorder). Else unchanged."""
    if args.record is None:
        return streams
    args.recorder = RoundRecorder(args.record, room, list(streams), notes=notes)
    if args.label is not None:
        args.recorder.label(args.label)
    return args.recorder.tee(streams, close_when_done=True)


def build_sources(args: argparse.Namespace, room: Room) -> tuple[dict[str, Stream], Optional[Mapping[str, float]]]:
    """Named (host_ts, line) streams and the per-link fixed thresholds for them (None in auto mode).
    Serial ports open on first read."""
    fixed = server_mode(args) == "fixed"
    if args.simulate:
        if len(room.transceivers) < 2:
            raise SystemExit(f"error: --simulate needs a room with at least two transceivers; {args.room} has "
                             f"{len(room.transceivers)} (try --room config/room_tr8.json)")
        streams = simulated_rig_streams(room, seed=args.seed, quiet_first_s=args.sim_quiet_first)
        args.gateways = dict(streams)   # the simulated rig takes raw mode requests too
        return record_rounds(args, room, streams, "dashboard.py --simulate"), None
    if args.node_gateway is not None:
        if len(room.transceivers) < 2:
            raise SystemExit(f"error: --node-gateway needs a room with at least two transceivers; {args.room} has "
                             f"{len(room.transceivers)} (try --room config/room_tr2.json)")
        args.key = kit_key(args)
        streams = {"gateway": gateway_source(args.node_gateway, args.baud)}
        args.gateways = dict(streams)   # raw mode requests go out through the gateway
        return record_rounds(args, room, streams, f"dashboard.py --node-gateway {args.node_gateway}"), None
    if args.simulate_room:
        thresholds = None
        if fixed:
            thresholds = load_link_thresholds(args.thresholds)
            if thresholds is None:
                print("calibrating the simulated empty room...", flush=True)
                thresholds = simulated_thresholds(room)
        quiet = looping_room_streams(room, default_walk(room), seed=args.seed, quiet_first_s=args.sim_quiet_first)
        if args.raw_log is not None:
            quiet = tee_sources(quiet, args.raw_log)
        return quiet, thresholds
    if args.gateway is not None:
        sources = gateway_streams(args.gateway, room, args.baud)
    else:
        sources = {name: serial_stream(path, args.baud) for name, path in parse_port_specs(args.port)}
    if args.raw_log is not None:
        sources = tee_sources(sources, args.raw_log)
    return sources, load_link_thresholds(args.thresholds) if fixed else None


def build_server(args: argparse.Namespace) -> FrameServer:
    """The server for the chosen source, not yet started. Raises ReplayError for unusable replay inputs.

    With --record, ``args.recorder`` is set to the RoundRecorder (None otherwise); it closes when the server stops."""
    speed = 1.0 if args.speed is None else args.speed
    page = args.page  # None: the server's default, web/index.html
    if args.replay is not None:
        return ReplayServer(load_log(args.replay), speed=speed, host=args.host, port=args.http_port, web_index=page)
    if args.replay_rounds is not None:
        return rounds_dashboard(args.replay_rounds, speed=speed, host=args.host, port=args.http_port, log=args.log,
                                web_index=page, kit_key=kit_key(args), possibly_occupied_s=hold_s(args))
    room = load_room(args.room)
    if args.replay_session is not None:
        thresholds = load_link_thresholds(args.thresholds)
        source = f"loaded from {args.thresholds}" if thresholds is not None else None
        server = session_dashboard(args.replay_session, room, speed=speed,
                                   margin=DEFAULT_MARGIN if args.margin is None else args.margin,
                                   thresholds=thresholds, thresholds_source=source,
                                   host=args.host, port=args.http_port, log=args.log)
        if page is not None:
            server._web_index = page  # session_dashboard takes no web_index; the page is read on each GET /
        return server
    args.recorder = None
    args.key = TEST_KIT_KEY
    args.gateways = {}
    sources, thresholds = build_sources(args, room)
    if args.simulate:
        source = "simulate (transceiver rig)"
    elif args.simulate_room:
        source = "simulate-room"
    elif args.gateway is not None:
        source = f"gateway {args.gateway}"
    elif args.node_gateway is not None:
        source = f"node gateway {args.node_gateway}"
    else:
        source = "ports " + ", ".join(n for n, _ in parse_port_specs(args.port))
    save_to = args.save_thresholds or args.thresholds
    mode = server_mode(args)
    if mode == "auto":
        thresholds_source = "none (auto mode)"
    else:
        thresholds_source = (f"loaded from {args.thresholds}" if args.thresholds else
                             ("simulated empty room" if args.simulate_room else "none"))
    notes = [] if args.node_gateway is None else [
        f"Round broadcasts are verified with the kit key, check {kit_key_check(args.key)}: every node's "
        "`scripts/node.py PORT key` must show the same check."]
    return DashboardServer(room, sources, thresholds, host=args.host, port=args.http_port, log=args.log,
                           allow_control=args.allow_control, thresholds_path=save_to, mode=mode, kit_key=args.key,
                           notes=notes, log_meta={"source": source, "thresholds_source": thresholds_source},
                           web_index=page, possibly_occupied_s=hold_s(args), gateways=args.gateways)


def read_labels(lines, recorder: RoundRecorder) -> None:
    """--record: each line is a label, optionally followed by key=value extras ("walking mark=A3"), in force from
    now. A bad line is reported and skipped; blank lines are ignored."""
    for line in lines:
        words = line.split()
        if not words:
            continue
        extra = dict(word.split("=", 1) for word in words[1:] if "=" in word)
        try:
            recorder.label(words[0], **extra)
        except ValueError as e:
            print(f"label not recorded: {e}", flush=True)
            continue
        print(f"label {words[0]} from now" + "".join(f" {k}={v}" for k, v in extra.items()), flush=True)


def _label_reader(recorder: RoundRecorder) -> None:
    try:
        read_labels(sys.stdin, recorder)
    except (OSError, ValueError):  # stdin closed or not readable: labels just stop
        pass


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    check_source(args)
    check_page(args)
    if args.auto_ports:
        args.port = resolve_auto_ports(load_room(args.room))
    try:
        server = build_server(args)
        server.start()
    except (ReplayError, OSError) as e:
        raise SystemExit(f"error: {e}")
    previous_sigterm = signal.signal(signal.SIGTERM, _stop_on_sigterm)
    print(f"dashboard at {server.url}  (listening on {server.address[0]})  Ctrl+C to stop", flush=True)
    recorder = getattr(args, "recorder", None)
    if recorder is not None:
        print(f"recording round broadcasts to {recorder.path}; type a label ({', '.join(LABELS)}) and Enter to "
              "switch", flush=True)
        threading.Thread(target=_label_reader, args=(recorder,), name="record-labels", daemon=True).start()
    replaying = args.replay or args.replay_session or args.replay_rounds
    if not controls_allowed(args.host, args.allow_control) and not replaying:
        print("commands disabled: not bound to localhost (calibrate and tuning need --allow-control)", flush=True)
    if server_mode(args) == "auto":
        print("auto mode (--auto-threshold, experimental): each link learns its own baseline "
              "(WARMUP for the first 10 s, no calibration needed)", flush=True)
    elif args.port and args.thresholds is None:
        print("no thresholds: every link shows UNCALIBRATED until calibrated (see links_live.py --calibrate)",
              flush=True)
    announced = False
    try:
        while True:
            if not announced and server.wait_finished(timeout=0):
                print("replay finished: serving the final state (Ctrl+C to stop)", flush=True)
                announced = True
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.stop()
        if recorder is not None:
            recorder.close()
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
