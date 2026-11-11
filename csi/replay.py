"""Replay: serve a logged dashboard run, or a recorded session, through the dashboard's HTTP interface.

    log = load_log("run.jsonl")                 # ReplayError names the file, and the line if it is corrupt
    server = ReplayServer(log, speed=1.0)       # speed=math.inf: as fast as possible
    server.start(); server.wait_finished(); server.stop()

A replayed log serves its logged payloads verbatim, at their logged times divided by ``speed``, through the
same /api/state and /api/events as a live run. The last payload is marked ``status.finished``.

    server = rounds_dashboard("run.rounds", speed=8)  # a round recording (csi/round_recording.py) through the
                                                      # live transceiver pipeline, every link in auto mode
"""
from __future__ import annotations

import dataclasses
import json
import math
import threading
import time
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence, Union

from .detect import DEFAULT_MARGIN, validate_margin
from .links import LinkId, calibrate_links_report
from .parser import CSIPacket
from .recording import META_NAME, Session, load_session
from .room import Room
from .round_recording import RecordingFormatError, load_rounds
from .server import DEFAULT_HOST, FRAME_INTERVAL_S, LOG_FORMAT, REPLAY_OFF, DashboardServer, FrameServer
from .simulate import DEFAULT_MAC

Pathlike = Union[str, Path]


class ReplayError(ValueError):
    """A replay input cannot be used; the message says what is wrong and where."""


@dataclass(frozen=True)
class ReplayLog:
    path: Path
    header: dict
    payloads: tuple[dict, ...]   # the logged state payloads in order; the last one is marked finished


def load_log(path: Pathlike) -> ReplayLog:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise ReplayError(f"cannot read replay log {path}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise ReplayError(f"cannot read replay log {path}: not UTF-8 text ({e})") from e
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ReplayError(f"{path} is empty: not a dashboard log")
    header = _json_line(path, 1, lines[0])
    if not isinstance(header, dict) or header.get("format") != LOG_FORMAT:
        raise ReplayError(f"{path} line 1 is not a dashboard log header (expected format {LOG_FORMAT!r})")
    payloads = [_payload(path, n, line) for n, line in enumerate(lines[1:], start=2)]
    if not payloads:
        raise ReplayError(f"{path} has a header but no state frames")
    payloads[-1]["status"]["finished"] = True
    return ReplayLog(path, header, tuple(payloads))


def _json_line(path: Path, n: int, line: str):
    try:
        return json.loads(line)
    except ValueError as e:
        raise ReplayError(f"{path} line {n} is corrupt: not valid JSON ({e})") from e


def _payload(path: Path, n: int, line: str) -> dict:
    obj = _json_line(path, n, line)
    status = obj.get("status") if isinstance(obj, dict) else None
    if (not isinstance(obj, dict) or not isinstance(obj.get("links"), list)
            or not isinstance(obj.get("heatmap"), dict) or not isinstance(status, dict)
            or not isinstance(status.get("time_s"), (int, float))):
        raise ReplayError(f"{path} line {n} is not a state payload (needs links, heatmap and status.time_s)")
    return obj


def _replay_commands(payload: dict) -> dict:
    """A logged payload as served by a replay: the live run's commands are not available here."""
    status = {**payload["status"], "commands": {"available": False, "reason": REPLAY_OFF}}
    return {**payload, "status": status}


def validate_speed(speed: float) -> float:
    """A replay speed: a positive factor, or math.inf for as fast as possible."""
    if not speed > 0:
        raise ValueError(f"replay speed must be positive, got {speed}")
    return float(speed)


class ReplayServer(FrameServer):
    """Serves a loaded log through the FrameServer endpoints, one payload at a time at the logged pace."""

    def __init__(self, log: ReplayLog, *, speed: float = 1.0, host: str = DEFAULT_HOST, port: int = 0,
                 web_index: Union[Path, str, None] = None) -> None:
        super().__init__(host, port, web_index)
        self._payloads: Sequence[dict] = [_replay_commands(p) for p in log.payloads]
        self._speed = validate_speed(speed)
        self._latest = json.dumps(self._payloads[0])
        self._feeder: threading.Thread | None = None

    def start(self) -> None:
        self._start_http()
        self._feeder = threading.Thread(target=self._feed, name="dashboard-replay", daemon=True)
        self._threads.append(self._feeder)
        self._feeder.start()

    def _feed(self) -> None:
        previous = None
        last = len(self._payloads) - 1
        for i, payload in enumerate(self._payloads):
            t = float(payload["status"]["time_s"])
            if previous is not None:
                wait_s = max(0.0, t - previous) / self._speed if self._speed != math.inf else 0.0
                if self._stop.wait(wait_s):
                    return
            previous = t
            text = json.dumps(payload)
            with self._lock:
                self._latest = text
                self._run_done = i == last
                self._frames.append(text, final=i == last)
        self._sources_done.set()

    def state_json(self) -> str:
        with self._lock:
            return self._latest


# ---------------------------------------------------------------- round recordings

def rounds_dashboard(path: Pathlike, *, speed: float = 1.0, room: Optional[Room] = None, host: str = DEFAULT_HOST,
                     port: int = 0, log: Optional[Pathlike] = None, frame_interval_s: float = FRAME_INTERVAL_S,
                     web_index: Union[Path, str, None] = None) -> DashboardServer:
    """A DashboardServer that replays a round recording through the live transceiver pipeline, not yet started.

    Each recorded source is fed as itself, its round broadcasts at their recorded host times, the gaps divided by
    ``speed`` (math.inf: as fast as possible). Every link is judged by its own adaptive baseline, as live. The room
    is the one recorded with the run unless ``room`` is given. Read-only, like every replay.
    """
    speed = validate_speed(speed)
    try:
        recording = load_rounds(path)
    except RecordingFormatError as e:
        raise ReplayError(str(e)) from e
    if not recording.broadcasts:
        raise ReplayError(f"{path} holds no round broadcasts")
    notes = []
    if recording.truncated_bytes:
        notes.append(f"the recording's last record was cut short: {recording.truncated_bytes} B left out")
    return DashboardServer(
        room or recording.room, recording.streams(speed), host=host, port=port, log=log,
        frame_interval_s=frame_interval_s, replay=True, mode="auto", web_index=web_index, notes=notes,
        log_meta={"source": f"replay-rounds {path}", "speed": speed, "thresholds_source": "none (auto mode)"})


# ---------------------------------------------------------------- recorded sessions

def session_dashboard(folder: Pathlike, room: Room, *, speed: float = 1.0, margin: float = DEFAULT_MARGIN,
                      thresholds: Optional[Mapping[str, float]] = None, thresholds_source: Optional[str] = None,
                      host: str = DEFAULT_HOST, port: int = 0, log: Optional[Pathlike] = None,
                      frame_interval_s: float = FRAME_INTERVAL_S) -> DashboardServer:
    """A DashboardServer that replays a recorded session through the live pipeline, not yet started.

    The session's packets (all recordings, in host-time order) are fed as receiver ``room.receivers[0]``
    at their recorded host times divided by ``speed``. A recording without a sender MAC, or with the
    simulator default, is the first sender's link. Without ``thresholds`` the links are calibrated on the
    session's first empty recording, as scripts/evaluate.py does, with ``margin``.
    """
    speed, margin = validate_speed(speed), validate_margin(margin)
    folder = Path(folder)
    if not (folder / META_NAME).exists():
        raise ReplayError(f"{folder} is not a session folder (no {META_NAME})")
    session = load_session(folder)
    receiver, default_mac = room.receivers[0].name, room.senders[0].mac
    packets = sorted(chain.from_iterable(_on_first_link(r.packets(), default_mac) for r in session.recordings),
                     key=lambda p: p.host_ts)
    if not packets:
        raise ReplayError(f"session {folder.name!r} has no packets")
    if thresholds is None:
        thresholds, thresholds_source = _calibrate(session, receiver, default_mac, margin)
    return DashboardServer(
        room, {receiver: _paced(packets, speed)}, thresholds, host=host, port=port, log=log,
        frame_interval_s=frame_interval_s, replay=True,
        log_meta={"source": f"replay-session {folder}", "speed": speed,
                  "thresholds_source": thresholds_source or "given to the replay"},
        notes=_link_notes(packets, room, receiver))


def _on_first_link(packets: Sequence[CSIPacket], default_mac: str) -> list[CSIPacket]:
    """Single-link recordings predate the mac column (or carry the simulator default): they are the first sender."""
    return [dataclasses.replace(p, mac=default_mac) if p.mac in ("", DEFAULT_MAC) else p for p in packets]


def _calibrate(session: Session, receiver: str, default_mac: str, margin: float) -> tuple[dict[str, float], str]:
    name = session.path.name
    empties = [r for r in session.recordings if r.label == "empty"]
    if not empties:
        raise ReplayError(f"session {name!r} has no empty recording to calibrate on; record the empty room first "
                          f"with: scripts/record.py --session {name} --label empty, or pass --thresholds")
    rec = empties[0]
    report = calibrate_links_report([(receiver, p) for p in _on_first_link(rec.packets(), default_mac)],
                                    margin=margin)
    if not report.thresholds:
        reasons = "; ".join(f"{label}: {why}" for label, why in report.skipped.items()) or "no packets"
        raise ReplayError(f"cannot calibrate on {rec.path.name}: {reasons}")
    return report.thresholds, (f"calibrated on {rec.path.name} (first empty recording of {name}) "
                               f"with margin {margin:g} x p99")


def _link_notes(packets: Sequence[CSIPacket], room: Room, receiver: str) -> list[str]:
    macs = sorted({p.mac for p in packets})
    labels = [LinkId(receiver, mac).label for mac in macs]
    if len(macs) == 1:
        return [f"single link: the recording has one sender link ({labels[0]}), so the heatmap uses that link "
                "only; the other links show NO_SIGNAL"]
    if len(macs) < len(room.senders):
        return [f"partial recording: {len(macs)} of {len(room.senders)} sender links ({', '.join(labels)}); "
                "the heatmap uses only those links"]
    return []


def _paced(packets: Sequence[CSIPacket], speed: float) -> Iterator[tuple[float, CSIPacket]]:
    """Packets at their recorded host times, scaled by ``speed``: each gap is slept off before the next packet."""
    previous = None
    for pkt in packets:
        if previous is not None and speed != math.inf:
            time.sleep(max(0.0, pkt.host_ts - previous) / speed)
        previous = pkt.host_ts
        yield pkt.host_ts, pkt
