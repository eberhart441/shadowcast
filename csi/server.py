"""HTTP dashboard server: per-receiver line streams in, JSON snapshot and SSE stream out. Standard library only.

    server = DashboardServer(room, sources, thresholds, host="127.0.0.1", port=8000)
    server.start()          # pump thread reads every source; HTTP server thread starts
    server.wait_finished()  # for finite sources: True once every source is exhausted
    server.stop()           # closes the sources and the HTTP server

``sources`` maps receiver name to a (host_ts, line) stream, the same seam as ``serial_stream``. A
source that raises is reported in ``status.receivers`` / ``status.errors`` and the others keep running.
Without ``thresholds`` every link is UNCALIBRATED (reported in ``status.messages``), not an error.

HTTP:
    GET /              the static page (web/index.html)
    GET /api/state     one payload: DashboardState.snapshot() plus the ``status`` extension below
    GET /api/events    text/event-stream; ``data: <payload json>`` at most ~5 Hz and a last event
                       (``status.finished`` true) when the sources finish; then the stream closes
    anything else      404

``status`` extension (added to DashboardState's status block):
    receivers          {name: connected | finished | failed}
    errors             {name: message} for failed receivers
    finished           True once every source has ended and its packets are drained
    calibrated_links   link labels with a threshold
    uncalibrated_links link labels without one
    messages           human-readable notes (uncalibrated links, failed receivers)

Frames: the pump emits a frame every ``frame_interval_s`` of packet time, so a fast finite stream still
yields intermediate events. Frames are kept in a bounded log; an event stream opened after the sources
finished replays the retained run from its oldest frame, one whose live counterpart sends the latest.
"""
from __future__ import annotations

import heapq
import ipaddress
import itertools
import json
import math
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, Optional, Sequence, TextIO, Union
from urllib.parse import urlsplit

from .dashboard import DashboardState
from .detect import LIVE_SCORER
from .live_calibration import CalibrationJob, RequestError, parse_request
from .links import LinkId, calibrate_links, save_thresholds
from .multisource import MultiSource
from .parser import CSIPacket, Stream
from .room import Room, Walk
from .simulate import simulate_room, simulate_room_links

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
FRAME_INTERVAL_S = 0.2     # packet-time between frames: 5 Hz
REORDER_STALL_S = 1.0      # wall-clock silence after which a receiver stops holding back the others
REORDER_STARTUP_S = 2.0    # wall-clock wait for a connected receiver that has not sent anything yet
WALL_CLOCK_TOLERANCE_S = 5.0  # packets stamped within this of the wall clock count as live
MIN_EVENT_GAP_S = 0.2      # wall-clock gap between two SSE events: at most ~5 Hz
FRAME_LOG_MAX = 1200       # frames kept for replay (4 minutes at 5 Hz)
CALIBRATION_JOIN_S = 30.0  # how long a run's final frame waits for a calibration still processing
LIVE_LAG_FRAMES = 5        # a live client further behind than this skips to the newest frame
KEEPALIVE_S = 15.0         # SSE comment sent when idle, so dead clients are noticed
WAIT_TICK_S = 0.2          # how often idle handlers re-check for shutdown
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SIM_WALK_SPEED_MPS = 0.5
SIM_T0 = 1_700_000_000.0  # synthetic host time of a simulated stream's first sample (as in csi.simulate)
SIM_CALIBRATION_S = 25.0
SIM_CALIBRATION_SEED = 10
LOG_FORMAT = "csi-dashboard-log"  # first line of a --log file; replay.py refuses other files
LOG_VERSION = 1
COMMAND_PATHS = frozenset({"/api/calibrate", "/api/calibrate/cancel", "/api/threshold",
                           "/api/thresholds/save", "/api/params"})
MAX_BODY_BYTES = 64 * 1024
DEFAULT_THRESHOLDS_FILE = "links_thresholds.json"   # working directory, unless --save-thresholds says otherwise
COMMAND_LOG_MAX = 20
LEGACY_SENDER_PREFIX = "1a:00:00:00:00:"  # sender addresses of the firmware before the team prefix 1a:0c:12:00:00
# live heatmap parameters: request key -> (heatmap attribute, allowed range)
PARAM_RANGES = {
    "sensitivity": ("sensitivity", (0.01, 10.0)),
    "decay": ("decay_tau", (0.05, 60.0)),
    "rise": ("rise_tau", (0.01, 10.0)),
    "falloff": ("falloff_m", (0.05, 2.0)),
}
LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost"})
LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})  # Host/Origin names always trusted
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
JSON_MEDIA_TYPE = "application/json"
CONTROL_OFF = ("control is disabled: the server is not bound to localhost; restart with --allow-control "
               "to enable calibration and tuning commands (no authentication)")
REPLAY_OFF = "not available in replay: this run replays a recording"
IDLE_CALIBRATION = {"state": "idle", "message": "", "clip": None, "clips": None, "seconds_left": 0,
                    "result": {}, "skipped": {}}


def is_loopback(host: str) -> bool:
    """True for a loopback bind address (127.0.0.0/8, ::1, localhost)."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def controls_allowed(host: str, allow_control: bool = False) -> bool:
    """Commands (calibrate, thresholds, parameters) run only on a loopback bind, unless explicitly allowed."""
    return allow_control or is_loopback(host)


def _split_host_port(value: str) -> tuple[str, Optional[int]]:
    """``host:port``, ``[v6]:port`` or a bare host, as a Host or Origin authority; port None when absent."""
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return value, None
        host, rest = value[1:end], value[end + 1:]
        port_text = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port_text = value.split(":")
    else:
        return value, None  # no port, or an unbracketed IPv6 literal
    try:
        return host, int(port_text)
    except ValueError:
        return host, None


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def legacy_sender_macs(room: Room) -> dict[str, str]:
    """Old-firmware sender address -> the configured sender with the same last byte.

    Boards still on the firmware from before the team prefix send from ``1a:00:00:00:00:XX``. Counting those
    packets as the configured ``...:XX`` sender's keeps such boards working with this code until every board is
    reflashed (a staged reflash). An old address that the room itself lists (the test fixtures) is left alone.
    """
    configured = {tx.mac.lower() for tx in room.senders}
    legacy = {}
    for tx in room.senders:
        old = LEGACY_SENDER_PREFIX + tx.mac[-2:].lower()
        if tx.mac and old not in configured:
            legacy[old] = tx.mac
    return legacy


def _finite_positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


class FrameLog:
    """Bounded, numbered log of rendered payloads, with blocking reads for event streams."""

    def __init__(self, maxlen: int = FRAME_LOG_MAX) -> None:
        self._frames: deque[tuple[int, str, bool]] = deque(maxlen=maxlen)
        self._seq = 0
        self._cond = threading.Condition()

    def append(self, text: str, final: bool = False) -> None:
        with self._cond:
            self._seq += 1
            self._frames.append((self._seq, text, final))
            self._cond.notify_all()

    def newest_seq(self) -> int:
        with self._cond:
            return self._seq

    def next_frame(self, cursor: int, timeout: float) -> Optional[tuple[int, str, bool]]:
        """First retained frame with seq >= cursor (an older cursor jumps to the oldest), or None on timeout."""
        with self._cond:
            if not self._frames or self._frames[-1][0] < cursor:
                self._cond.wait(timeout)
            for item in self._frames:
                if item[0] >= cursor:
                    return item
        return None


class _Reorder:
    """Releases packets from all receivers in host-time order, so the state does not depend on thread timing.

    Each receiver's packets arrive in order, so once every active receiver has sent packets up to time T,
    everything at or before T is complete and can be released. A receiver is active while it is connected
    and has sent within ``stall_s`` of wall time; a connected receiver that has never sent is waited for
    during ``startup_s`` of wall time. So a stalled receiver cannot freeze the others, and a fast receiver
    cannot get ahead of a slow one that is still running. ``flush`` releases everything left.
    """

    def __init__(self, stall_s: float = REORDER_STALL_S, startup_s: float = REORDER_STARTUP_S) -> None:
        self._stall_s, self._startup_s = stall_s, startup_s
        self._heap: list[tuple[float, str, int, CSIPacket]] = []
        self._newest: dict[str, float] = {}
        self._last_wall: dict[str, float] = {}
        self._seq = itertools.count()
        self._t0 = time.monotonic()

    def add(self, receiver: str, pkt: CSIPacket) -> None:
        heapq.heappush(self._heap, (pkt.host_ts, receiver, next(self._seq), pkt))
        self._newest[receiver] = max(self._newest.get(receiver, pkt.host_ts), pkt.host_ts)
        self._last_wall[receiver] = time.monotonic()

    def pop_ready(self, connected: set[str], flush: bool = False) -> list[tuple[str, CSIPacket]]:
        threshold = float("inf") if flush else self._threshold(connected)
        if threshold is None:
            return []
        out = []
        while self._heap and self._heap[0][0] <= threshold:
            _, receiver, _, pkt = heapq.heappop(self._heap)
            out.append((receiver, pkt))
        return out

    def _threshold(self, connected: set[str]) -> Optional[float]:
        """Newest time that is complete across the active receivers; None to wait for a receiver to start."""
        now = time.monotonic()
        marks = []
        for receiver in connected:
            if receiver in self._newest:
                if now - self._last_wall[receiver] <= self._stall_s:
                    marks.append(self._newest[receiver])
            elif now - self._t0 < self._startup_s:
                return None
        return min(marks) if marks else float("inf")


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    dashboard: "DashboardServer"


class _Handler(BaseHTTPRequestHandler):
    server_version = "csi-dashboard"
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args) -> None:  # keep the console for the operator
        pass

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        dash = self.server.dashboard
        if path == "/api/state":
            self._send(200, "application/json; charset=utf-8", dash.state_json().encode())
        elif path == "/api/events":
            self._events(dash)
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", dash.index_bytes())
        else:
            view = dash.view(path)
            if view is None:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
            else:
                self._json(200, view)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        dash = self.server.dashboard
        if path not in COMMAND_PATHS:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")
            return
        refusal = dash.refusal(self.headers)
        if refusal is not None:
            self._json(*refusal)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"request body must be 0 to {MAX_BODY_BYTES} bytes"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "request body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "request body must be a JSON object"})
            return
        status, obj = dash.command(path, body)
        self._json(status, obj)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self, dash: "DashboardServer") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            dash.stream_events(self.wfile)
        except OSError:  # the client went away mid-stream: normal, not an error
            pass
        self.close_connection = True


class FrameServer:
    """The HTTP side shared by the live server and replay: rendered JSON payloads in a FrameLog, served
    over /api/state (``state_json``), /api/events (``stream_events``) and /. Subclasses produce the frames."""

    def __init__(self, host: str, port: int, web_index: Optional[Path] = None,
                 trusted_host: Optional[str] = None) -> None:
        """``trusted_host``: the --host value when network control was allowed; its name is accepted in Host."""
        self._lock = threading.Lock()
        self._trusted_host = trusted_host
        self._frames = FrameLog()
        self._run_done = False       # set under the lock, just before the final frame
        self._sources_done = threading.Event()
        self._stop = threading.Event()
        self._stopped = False
        self._web_index = WEB_DIR / "index.html" if web_index is None else Path(web_index)
        self._httpd = _Httpd((host, port), _Handler)
        self._httpd.dashboard = self
        self._threads: list[threading.Thread] = []
        self._http_started = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{port}"

    def refusal(self, headers) -> Optional[tuple[int, dict]]:
        """Why a POST command must not run, as (HTTP status, body), or None when it may.

        A cross-site page can only send a simple request (no preflight, which this server never grants), so the
        media type must equal application/json (parameters after ';' ignored). The Host must name this server
        (loopback names, or the --host value with network control), so DNS rebinding is refused; an Origin, when
        sent, must be an http origin of this server.
        """
        ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != JSON_MEDIA_TYPE:
            return 415, {"error": "commands take a JSON body: send Content-Type: application/json"}
        host = headers.get("Host") or ""
        if not self._authority_allowed(host):
            return 403, {"error": f"command refused: Host {host!r} is not this dashboard; use localhost, "
                                  f"127.0.0.1 or [::1] with port {self.address[1]}"}
        origin = headers.get("Origin")
        if origin is not None:
            parts = urlsplit(origin)
            if parts.scheme != "http" or not self._authority_allowed(parts.netloc):
                return 403, {"error": f"command refused: Origin {origin!r} is not allowed for this dashboard"}
        return None

    def _authority_allowed(self, authority: str) -> bool:
        """True for ``host:port`` naming this server's bound port on a trusted host name."""
        host, port = _split_host_port(authority)
        if port is None or port != self._httpd.server_address[1]:
            return False
        name = host.lower()
        if name in LOOPBACK_HOST_NAMES:
            return True
        if self._trusted_host is None:
            return False
        if self._trusted_host in WILDCARD_HOSTS:
            return _is_ip_literal(name)  # a DNS name cannot be rebound onto an address, so IP literals are the trusted network form
        return name == self._trusted_host.lower()

    def _start_http(self) -> None:
        thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                  name="dashboard-http", daemon=True)
        self._http_started = True
        self._threads.append(thread)
        thread.start()

    def wait_finished(self, timeout: Optional[float] = None) -> bool:
        """True once every source has ended and the final frame is in the log."""
        return self._sources_done.wait(timeout)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._http_started:
            self._httpd.shutdown()
        self._httpd.server_close()
        self._close_sources()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._sources_done.set()

    def _close_sources(self) -> None:
        """Subclass hook: release the frame producers (ports, timers) before the threads are joined."""

    def state_json(self) -> str:
        raise NotImplementedError

    def command(self, path: str, body: Mapping) -> tuple[int, dict]:
        """POST command (see COMMAND_PATHS). The base server, a replay, takes none."""
        return 409, {"error": REPLAY_OFF}

    def view(self, path: str) -> Optional[dict]:
        """GET of a server-specific resource (none in the base server): None means not found."""
        return None

    def __enter__(self) -> "FrameServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def index_bytes(self) -> bytes:
        try:
            return self._web_index.read_bytes()
        except OSError:
            return b"<!doctype html><title>dashboard</title><p>web/index.html is missing</p>"

    # ------------------------------------------------------------------ event stream

    def stream_events(self, wfile: BinaryIO) -> None:
        """Write SSE events until the final frame, server shutdown, or a client disconnect (OSError)."""
        with self._lock:
            run_done = self._run_done
        cursor = 1 if run_done else max(1, self._frames.newest_seq())
        last_sent = 0.0
        while not self._stop.is_set():
            if not self._run_done:
                newest = self._frames.newest_seq()
                if newest - cursor >= LIVE_LAG_FRAMES:
                    cursor = newest
            item = self._frames.next_frame(cursor, WAIT_TICK_S)
            if item is None:
                if time.monotonic() - last_sent >= KEEPALIVE_S:
                    wfile.write(b": keepalive\n\n")
                    wfile.flush()
                    last_sent = time.monotonic()
                continue
            seq, text, final = item
            gap = MIN_EVENT_GAP_S - (time.monotonic() - last_sent)
            if gap > 0:
                self._stop.wait(gap)
            wfile.write(f"data: {text}\n\n".encode())
            wfile.flush()
            last_sent, cursor = time.monotonic(), seq + 1
            if final:
                return


class DashboardServer(FrameServer):
    """The live pipeline (sources -> DashboardState -> frames) behind the FrameServer HTTP interface.

    ``log`` appends the frame stream to a file: a header line (room, thresholds, heatmap parameters, start
    time, ``log_meta``, ``notes``) and then one payload per line, exactly as /api/events sends them.
    ``notes`` are extra human-readable lines for ``status.messages``.
    """

    def __init__(self, room: Room, sources: Mapping[str, Stream], thresholds: Optional[Mapping[str, float]] = None,
                 *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 frame_interval_s: float = FRAME_INTERVAL_S, web_index: Optional[Path] = None,
                 log: Optional[Union[str, Path]] = None, log_meta: Optional[Mapping] = None,
                 notes: Sequence[str] = (), allow_control: bool = False,
                 thresholds_path: Optional[Union[str, Path]] = None, replay: bool = False,
                 mode: str = "fixed", **heatmap_params) -> None:
        network_control = allow_control and not is_loopback(host)
        super().__init__(host, port, web_index, trusted_host=host if network_control else None)
        self._room = room
        self._state = DashboardState(room, dict(thresholds or {}), mode=mode, **heatmap_params)
        self._expected = [LinkId(rx.name, tx.mac) for rx, tx in room.links()]
        self._labels = {link.label for link in self._expected}
        self._legacy_macs = legacy_sender_macs(room)
        self._controls_on = controls_allowed(host, allow_control)
        self._replay = replay
        self._thresholds_path = Path(thresholds_path) if thresholds_path else Path(DEFAULT_THRESHOLDS_FILE)
        self._calib: Optional[CalibrationJob] = None
        self._calib_thread: Optional[threading.Thread] = None
        self._commands: deque[str] = deque(maxlen=COMMAND_LOG_MAX)
        # a stream for a disabled node (room config "enabled": false) is never read: it has no links
        self._ms = MultiSource({name: s for name, s in sources.items() if name not in room.disabled})
        self._notes = list(notes)
        self._log_path = None if log is None else Path(log)
        self._log_file: Optional[TextIO] = None
        self._log_header = {"format": LOG_FORMAT, "version": LOG_VERSION, "room": asdict(room),
                            "thresholds": dict(thresholds or {}), "mode": mode, "frame_interval_s": frame_interval_s,
                            "heatmap": dict(heatmap_params), "notes": list(notes), **dict(log_meta or {})}
        self._frame_s = frame_interval_s
        self._next_frame_at: Optional[float] = None
        self._last_host: Optional[float] = None
        self._last_mono: Optional[float] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._pipeline_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._log_path is not None:
            self._open_log()
        self._ms.start()
        self._pump_thread = threading.Thread(target=self._pump, name="dashboard-pump", daemon=True)
        self._threads.append(self._pump_thread)
        self._pump_thread.start()
        self._start_http()

    def stop(self) -> None:
        super().stop()
        self._close_log()

    def _close_sources(self) -> None:
        self._ms.close()

    def _open_log(self) -> None:
        self._log_file = open(self._log_path, "w", encoding="utf-8")
        self._log_file.write(json.dumps({**self._log_header, "started": time.time()}) + "\n")
        self._log_file.flush()

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------ ingest

    def _pump(self) -> None:
        reorder = _Reorder()
        try:
            while not self._stop.is_set():
                for receiver, pkt in self._ms.drain(timeout=0.05):
                    reorder.add(receiver, pkt)
                self._feed(reorder.pop_ready(self._connected_receivers()))
                self._idle_frame()
                if self._ms.finished():
                    break
            if not self._stop.is_set():
                self._feed(reorder.pop_ready(self._connected_receivers(), flush=True))
                self._finish_run()
        except _PipelineError as e:
            self._fail_run(e.receiver, e.__cause__ or e)
        except Exception as e:  # not from one packet: the pump itself failed
            self._fail_run(None, e)
        finally:
            self._sources_done.set()

    def _finish_run(self) -> None:
        self._join_calibration()
        with self._lock:
            if self._calib is not None and self._calib.active:
                self._calib.abort("the sources ended before the calibration recording finished")
            self._run_done = True
            self._emit(self._last_host or 0.0, final=True)

    def _fail_run(self, receiver: Optional[str], error: BaseException) -> None:
        """The pipeline raised: name the receiver (or 'pipeline'), stop the readers, and end the run with a final frame.

        Nothing drains the sources after this, so they are closed; a client sees the error and the end of the run
        instead of a stale picture that looks live.
        """
        message = str(error) or type(error).__name__
        if receiver is not None:
            self._ms.mark_failed(receiver, message)
        else:
            self._pipeline_errors["pipeline"] = message
        self._ms.close()
        self._join_calibration()
        with self._lock:
            if self._calib is not None and self._calib.active:
                self._calib.abort(f"the pipeline failed: {message}")
            self._run_done = True
            try:
                self._emit(self._last_host or 0.0, final=True)
            except Exception:  # the state itself is broken: a minimal final frame still ends the run
                fallback = {"links": [], "heatmap": {}, "status": {
                    "time_s": self._last_host or 0.0, "finished": True, "errors": self._errors(),
                    "messages": [f"run ended: the pipeline failed: {message}"]}}
                self._frames.append(json.dumps(fallback), final=True)

    def _connected_receivers(self) -> set[str]:
        return {name for name, s in self._ms.status().items() if _receiver_state(s) == "connected"}

    def _feed(self, batch: list[tuple[str, CSIPacket]]) -> None:
        if not batch:
            return
        with self._lock:
            for receiver, pkt in batch:
                try:
                    self._push_packet(receiver, pkt)
                except Exception as e:
                    raise _PipelineError(receiver) from e
            self._last_mono = time.monotonic()

    def _push_packet(self, receiver: str, pkt: CSIPacket) -> None:
        """Caller holds the lock. A packet from an old-firmware sender address counts as its configured sender's
        (``legacy_sender_macs``), for the links, the calibration and the heatmap alike."""
        if pkt.mac in self._legacy_macs:
            pkt = replace(pkt, mac=self._legacy_macs[pkt.mac])
        self._state.push(receiver, pkt)
        if self._calib is not None and self._calib.active:
            self._calib.observe(receiver, pkt, time.monotonic())
        if self._last_host is None or pkt.host_ts > self._last_host:
            self._last_host = pkt.host_ts
        if self._next_frame_at is None:
            self._next_frame_at = pkt.host_ts + self._frame_s
        elif pkt.host_ts >= self._next_frame_at:
            self._emit(pkt.host_ts)
            self._next_frame_at = pkt.host_ts + self._frame_s

    def _errors(self) -> dict[str, str]:
        return {**self._ms.errors(), **self._pipeline_errors}

    def _idle_frame(self) -> None:
        """With no packets arriving, keep frames coming on the clock so silent links turn NO_SIGNAL."""
        with self._lock:
            now = self._clock()
            if self._calib is not None and self._calib.active:
                self._calib.tick(now, time.monotonic())
            if now is not None and self._next_frame_at is not None and now >= self._next_frame_at:
                self._emit(now)
                self._next_frame_at = now + self._frame_s

    def _clock(self) -> Optional[float]:
        """Host time now. Live hardware stamps packets with the wall clock, so while those run the clock
        moves on between packets (silent links turn NO_SIGNAL). Other timestamps (replays, simulations)
        only move with the packets, which keeps their results independent of thread timing."""
        if self._last_host is None:
            return None
        wall_stamped = abs(time.time() - self._last_host) <= WALL_CLOCK_TOLERANCE_S
        if self._ms.finished() or not wall_stamped:
            return self._last_host
        return self._last_host + (time.monotonic() - (self._last_mono or time.monotonic()))

    def _emit(self, now: float, final: bool = False) -> None:
        """Append a frame at host time ``now``. Caller holds the lock."""
        text = json.dumps(self._payload(now))
        self._frames.append(text, final=final)
        if self._log_file is not None:
            self._log_file.write(text + "\n")
            self._log_file.flush()

    # ------------------------------------------------------------------ payload

    def _payload(self, now: float) -> dict:
        """Caller holds the lock: DashboardState advances its smoothing to ``now``."""
        payload = self._state.snapshot(now)
        payload["status"].update(self._status_extension(now, payload["links"]))
        return payload

    def _status_extension(self, now: float, links: Sequence[Mapping]) -> dict:
        receivers = {name: _receiver_state(s) for name, s in self._ms.status().items()}
        errors = self._errors()
        calibrated = sorted(self._state.thresholds)
        adaptive = sorted(row["id"] for row in links if row.get("mode") == "auto")  # no threshold needed
        uncalibrated = sorted(self._labels - set(calibrated) - set(adaptive))
        silent = sorted(row["id"] for row in links if row["id"] in uncalibrated and row["age_s"] is None)
        heard = [label for label in uncalibrated if label not in silent]
        messages = list(self._notes)
        if heard:
            messages.append(f"{len(heard)} link(s) UNCALIBRATED ({', '.join(heard)}): no threshold. "
                            "Pass --thresholds links.json, or calibrate on an empty room first.")
        if silent:  # a link that never sent a packet has nothing to calibrate: say so, not UNCALIBRATED
            where = "in this recording" if self._replay else "yet"
            messages.append(f"{len(silent)} link(s) have no data {where} ({', '.join(silent)}).")
        messages.extend(f"{'pipeline' if name == 'pipeline' else f'receiver {name}'} failed: {msg}"
                        for name, msg in sorted(errors.items()))
        calibration = self._calib_status(now)
        if self._calib is not None:
            messages.extend(f"calibration skipped {label}: {why}" for label, why in self._calib.skipped.items())
        messages.extend(f"command: {line}" for line in self._commands)
        return {
            "receivers": receivers,
            "errors": errors,
            "finished": self._run_done,
            "calibrated_links": calibrated,
            "adaptive_links": adaptive,
            "uncalibrated_links": uncalibrated,
            "messages": messages,
            "calibration": calibration,
            "commands": self._commands_status(),
        }

    def _commands_status(self) -> dict:
        if not self._controls_on:
            return {"available": False, "reason": CONTROL_OFF}
        if self._replay:
            return {"available": False, "reason": REPLAY_OFF}
        return {"available": True, "reason": None}

    def _calib_status(self, now: Optional[float]) -> dict:
        if self._calib is None:
            return dict(IDLE_CALIBRATION)
        return self._calib.status(now, time.monotonic())

    def state_json(self) -> str:
        with self._lock:
            now = self._clock()
            return json.dumps(self._payload(0.0 if now is None else now))

    # ------------------------------------------------------------------ commands

    def view(self, path: str) -> Optional[dict]:
        if path != "/api/thresholds" or self._replay:
            return None
        with self._lock:
            return {"thresholds": self._state.thresholds, "path": str(self._thresholds_path.resolve())}

    def command(self, path: str, body: Mapping) -> tuple[int, dict]:
        """Run one POST command under the lock. Returns (HTTP status, JSON object)."""
        with self._lock:
            if not self._controls_on:
                return 403, {"error": CONTROL_OFF}
            if self._replay:
                return 409, {"error": REPLAY_OFF}
            handlers = {
                "/api/calibrate": self._cmd_calibrate,
                "/api/calibrate/cancel": self._cmd_cancel,
                "/api/threshold": self._cmd_threshold,
                "/api/thresholds/save": self._cmd_save,
                "/api/params": self._cmd_params,
            }
            try:
                return handlers[path](body)
            except RequestError as e:
                return 400, {"error": str(e)}
            except Exception as e:  # a bad request must never kill the handler thread
                return 500, {"error": f"internal error: {type(e).__name__}: {e}"}

    def _cmd_calibrate(self, body: Mapping) -> tuple[int, dict]:
        if self._calib is not None and self._calib.active:
            return 409, {"error": "a calibration is already running", "calibration": self._calib_status(self._clock())}
        request = parse_request(body)
        self._calib = CalibrationJob(request, self._expected, now_mono=time.monotonic(),
                                     current=lambda: self._state.thresholds, apply=self._apply_threshold,
                                     defer=self._defer_calibration)
        self._commands.append(f"calibrate: {request['clips']} clip(s) of {request['seconds']:g} s, margin "
                              f"{request['margin']:g}, lead-in {request['lead_s']:g} s, "
                              f"{'noisy room' if request['noisy'] else 'quiet room'}")
        return 202, {"calibration": self._calib_status(self._clock())}

    def _defer_calibration(self, compute, complete) -> None:
        """Run a finished recording's (slow) report off the lock, so requests and frames keep flowing.

        Called under the lock by the job; the thread takes the lock again only to apply the outcome.
        """
        def run() -> None:
            outcome = compute()
            with self._lock:
                complete(outcome)

        self._calib_thread = threading.Thread(target=run, name="dashboard-calibration", daemon=True)
        self._calib_thread.start()

    def _join_calibration(self) -> None:
        """Wait (without the lock) for a calibration still processing, so a run's final frame shows its result."""
        thread = self._calib_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=CALIBRATION_JOIN_S)

    def _cmd_cancel(self, body: Mapping) -> tuple[int, dict]:
        if self._calib is None or not self._calib.cancel():
            return 409, {"error": "no calibration is running"}
        self._commands.append("calibrate cancelled")
        return 200, {"calibration": self._calib_status(self._clock())}

    def _cmd_threshold(self, body: Mapping) -> tuple[int, dict]:
        unknown = sorted(set(body) - {"link", "value"})
        if unknown:
            raise RequestError(f"unknown field(s): {', '.join(unknown)}; expected link and value")
        link = body.get("link")
        if not isinstance(link, str) or link not in self._labels:
            raise RequestError(f"unknown link {link!r}; links are {', '.join(sorted(self._labels))}")
        if "value" in body and body["value"] is None:  # explicit null: back to the adaptive baseline (auto mode)
            self._state.set_threshold(link, None)
            self._commands.append(f"threshold {link} = auto")
            return 200, {"link": link, "threshold": None}
        value = body.get("value")
        if not _finite_positive(value):
            raise RequestError(f"value must be a positive number, or null for auto, got {value!r}")
        self._state.set_threshold(link, float(value))
        self._commands.append(f"threshold {link} = {float(value):.4g}")
        return 200, {"link": link, "threshold": float(value)}

    def _cmd_save(self, body: Mapping) -> tuple[int, dict]:
        thresholds = self._state.thresholds
        if not thresholds:
            return 409, {"error": "no thresholds to save: calibrate or set a link threshold first"}
        path = self._thresholds_path.resolve()
        try:
            # record the live scorer, so the next start does not warn that the file may predate it: thresholds
            # calibrated or Set here are on its scale (one loaded from an older file is not; recalibrate it first)
            save_thresholds(path, thresholds, {"scorer": LIVE_SCORER.as_dict()})
        except OSError as e:
            return 500, {"error": f"cannot write {path}: {e.strerror or e}"}
        self._commands.append(f"saved {len(thresholds)} threshold(s) to {path}")
        return 200, {"path": str(path), "thresholds": thresholds}

    def _cmd_params(self, body: Mapping) -> tuple[int, dict]:
        unknown = sorted(set(body) - set(PARAM_RANGES))
        if unknown:
            raise RequestError(f"unknown parameter(s): {', '.join(unknown)}; allowed: {', '.join(PARAM_RANGES)}")
        if not body:
            raise RequestError(f"no parameters given; allowed: {', '.join(PARAM_RANGES)}")
        changes: dict[str, float] = {}
        for key, value in body.items():
            attr, (lo, hi) = PARAM_RANGES[key]
            if not _finite_positive(value):
                raise RequestError(f"{key} must be a positive number, got {value!r}")
            if not lo <= value <= hi:
                raise RequestError(f"{key} must be between {lo:g} and {hi:g}, got {value:g}")
            changes[attr] = float(value)
        self._state.set_params(**changes)
        self._commands.append("parameters: " + ", ".join(f"{k}={float(v):g}" for k, v in body.items()))
        return 200, {"parameters": self._state.parameters()}

    def _apply_threshold(self, label: str, value: float) -> None:
        """Calibration's apply hook; the caller holds the lock."""
        self._state.set_threshold(label, value)
        self._commands.append(f"threshold {label} = {value:.4g} (calibration)")



class _PipelineError(Exception):
    """A packet from ``receiver`` made the pipeline raise; the cause is chained."""

    def __init__(self, receiver: str) -> None:
        super().__init__(receiver)
        self.receiver = receiver


def _receiver_state(status: str) -> str:
    if status.startswith("error"):
        return "failed"
    if status in ("done", "stopped"):
        return "finished"
    return "connected"


# ---------------------------------------------------------------------- simulated sources

def simulated_thresholds(room: Room, seed: int = SIM_CALIBRATION_SEED,
                         duration_s: float = SIM_CALIBRATION_S) -> dict[str, float]:
    """Per-link thresholds from a short simulated empty room: the calibration for the simulator."""
    return calibrate_links(list(simulate_room_links(room=room, person=None, duration_s=duration_s, seed=seed)))


def default_walk(room: Room, speed_mps: float = SIM_WALK_SPEED_MPS) -> Walk:
    """A looping walk round the room: left wall, top edge, right wall, bottom edge."""
    waypoints = (room.wall_point("left"), room.wall_point("top"), room.wall_point("right"),
                 room.wall_point("bottom"))
    return Walk(waypoints, speed_mps=speed_mps, loop=True)


def walk_period_s(walk: Walk) -> float:
    pts = list(walk.waypoints) + [walk.waypoints[0]]
    length = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
    return length / walk.speed_mps


def looping_room_streams(room: Room, person: Walk, *, seed: int = 0, quiet_first_s: float = 0.0,
                         realtime: bool = True) -> dict[str, Stream]:
    """Endless streams, one per receiver, of a person repeating ``person``'s loop.

    ``quiet_first_s`` seconds of an empty room come first, so a calibration can run before anyone walks.
    The timeline is one continuous run: cycle k starts where cycle k-1 ended, on both the host clock and
    the device counter, so the receivers never look rebooted at a loop boundary.
    """
    period = walk_period_s(person)
    return {rx.name: _looping(rx.name, room, person, period, seed, quiet_first_s, realtime)
            for rx in room.receivers}


def _looping(name: str, room: Room, person: Walk, period: float, seed: int, quiet_first_s: float,
             realtime: bool) -> Iterator[tuple[float, str]]:
    start_s = 0.0
    for cycle in itertools.count():
        if cycle == 0 and quiet_first_s > 0:
            cycle_person, duration_s = None, quiet_first_s
        else:
            cycle_person, duration_s = person, period
        streams = simulate_room(cycle_person, room=room, duration_s=duration_s, seed=seed, realtime=realtime,
                                t0=SIM_T0 + start_s, device_offset_us=round(start_s * 1e6))
        lines = streams[name]
        yield from (lines if cycle == 0 else itertools.islice(lines, 1, None))  # one header is enough
        start_s += duration_s
