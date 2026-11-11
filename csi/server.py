"""HTTP dashboard server: per-receiver line streams in, JSON snapshot and SSE stream out. Standard library only.

    server = DashboardServer(room, sources, thresholds, host="127.0.0.1", port=8000)
    server.start()          # pump thread reads every source; HTTP server thread starts
    server.wait_finished()  # for finite sources: True once every source is exhausted
    server.stop()           # closes the sources and the HTTP server

``sources`` maps receiver name to a (host_ts, line) stream, the same seam as ``serial_stream``, or, for the
transceiver pipeline, a source name (a gateway) to a (host_ts, round broadcast bytes) stream. A
source that raises is reported in ``status.receivers`` / ``status.errors`` and the others keep running.
Without ``thresholds`` every link is UNCALIBRATED (reported in ``status.messages``), not an error.

HTTP:
    GET /              the static page (web/index.html)
    GET /api/state     one payload: DashboardState.snapshot() plus the ``status`` extension below
    GET /api/events    text/event-stream; ``data: <payload json>`` at most ~5 Hz and a last event
                       (``status.finished`` true) when the sources finish; then the stream closes. A room with
                       more than FRAME_LINKS_MAX links sends payloads without ``links`` (ADR-0024)
    GET /api/link?id=<label>   {link, time_s}: one link's full row on demand; 400 without id, 404 if unknown
    POST /api/raw-mode {"link": "n1<-n5", "on": true|false}: raw mode for one transceiver link (csi/raw_mode.py),
                       sent through a gateway in ``gateways``; 200 {raw_mode, sent, via}, 400 for a bad body
                       or unknown link, 409 {error, reason} when refused (byte budget, a node's limit, the node not
                       heard yet, no gateway that can send, a replayed run). A command like the others: loopback only
                       unless --allow-control (ADR-0015)
    anything else      404

``status`` extension (added to DashboardState's status block):
    receivers          {name: connected | finished | failed}
    errors             {name: message} for failed receivers
    finished           True once every source has ended and its packets are drained
    calibrated_links   link labels with a threshold
    uncalibrated_links link labels without one
    messages           human-readable notes (uncalibrated links, failed receivers, unknown nodes, rejected or
                       dropped round broadcasts)
    links_in_frames    False when event-stream frames leave out the per-link rows

Frames are captured under the lock (``DashboardState.capture``) and rendered to JSON after the pump releases it.
The pump emits a frame every ``frame_interval_s`` of packet time, so a fast finite stream still
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
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, Optional, Sequence, TextIO, Union
from urllib.parse import parse_qs, urlsplit

from .dashboard import Capture, DashboardState
from .gateway_records import LOSS_COUNTERS

from .live_calibration import CalibrationJob, RequestError, parse_request
from .links import calibrate_links, room_link_ids, save_thresholds
from .multisource import MultiSource
from .parser import CSIPacket, Stream
from .raw_mode import RawModeRefused, SendingGateway, raw_messages
from .room import Room, Walk
from .room_state import POSSIBLY_OCCUPIED_S
from .round_auth import TEST_KIT_KEY
from .rounds import AUTH, DUPLICATE, QUEUE_OVERFLOW, HeardBroadcast
from .simulate import SimulatedRig, simulate_room, simulate_room_links

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
FRAME_INTERVAL_S = 0.2     # packet-time between frames: 5 Hz
REORDER_STALL_S = 1.0      # wall-clock silence after which a receiver stops holding back the others
REORDER_STARTUP_S = 2.0    # wall-clock wait for a connected receiver that has not sent anything yet
WALL_CLOCK_TOLERANCE_S = 5.0  # packets stamped within this of the wall clock count as live
MIN_EVENT_GAP_S = 0.2      # wall-clock gap between two SSE events: at most ~5 Hz
FRAME_LOG_MAX = 1200       # frames kept for replay (4 minutes at 5 Hz)
FRAME_LINKS_MAX = 64       # a room with more links sends frames without per-link rows (GET /api/link has them)
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
                           "/api/thresholds/save", "/api/params", "/api/raw-mode"})
MAX_BODY_BYTES = 64 * 1024
DEFAULT_THRESHOLDS_FILE = "links_thresholds.json"   # working directory, unless --save-thresholds says otherwise
COMMAND_LOG_MAX = 20
SEND_TIMEOUT_S = 2.0       # a command's write to a gateway's port that takes longer is given up (503 "timeout")
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
        elif path == "/api/link":
            ids = parse_qs(urlsplit(self.path).query).get("id")
            self._json(*dash.link_detail(ids[0] if ids else None))
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

    def link_detail(self, label: Optional[str]) -> tuple[int, dict]:
        """GET /api/link?id=<label>: one link's full row on demand, as (HTTP status, JSON object)."""
        return 404, {"error": f"per-link detail is {REPLAY_OFF}"}

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
    ``notes`` are extra human-readable lines for ``status.messages``. Round broadcasts are verified with
    ``kit_key`` (csi/round_auth.py; by default the published test key the simulator signs with).
    ``gateways`` maps a source name to the object that can send for it (a node gateway, the simulated rig):
    raw mode requests go out through the one that last delivered the target node's round broadcast.
    ``possibly_occupied_s`` is the transceiver room state's possibly-occupied hold (csi/room_state.py).
    """

    def __init__(self, room: Room, sources: Mapping[str, Stream], thresholds: Optional[Mapping[str, float]] = None,
                 *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 frame_interval_s: float = FRAME_INTERVAL_S, web_index: Optional[Path] = None,
                 log: Optional[Union[str, Path]] = None, log_meta: Optional[Mapping] = None,
                 notes: Sequence[str] = (), allow_control: bool = False,
                 thresholds_path: Optional[Union[str, Path]] = None, replay: bool = False,
                 mode: str = "fixed", kit_key: bytes = TEST_KIT_KEY,
                 possibly_occupied_s: float = POSSIBLY_OCCUPIED_S,
                 gateways: Optional[Mapping[str, SendingGateway]] = None, **heatmap_params) -> None:
        network_control = allow_control and not is_loopback(host)
        super().__init__(host, port, web_index, trusted_host=host if network_control else None)
        self._room = room
        self._state = DashboardState(room, dict(thresholds or {}), mode=mode, kit_key=kit_key,
                                     possibly_occupied_s=possibly_occupied_s, **heatmap_params)
        self._expected = [link_id for link_id, _, _ in room_link_ids(room)]
        self._labels = {link.label for link in self._expected}
        self._controls_on = controls_allowed(host, allow_control)
        self._gateways = dict(gateways or {})
        # sources that count what they lost before the host (a node gateway): status.broadcasts.gateways
        self._lossy = {name: obj for name, obj in {**dict(sources), **self._gateways}.items()
                       if callable(getattr(obj, "losses", None))}
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
        self._links_in_frames = len(self._expected) <= FRAME_LINKS_MAX
        self._pending: list[tuple[Capture, dict, bool]] = []   # frames captured under the lock, not yet rendered

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
                self._count_dropped()
                self._feed(reorder.pop_ready(self._connected_receivers()))
                self._idle_frame()
                if self._ms.finished():
                    break
            if not self._stop.is_set():
                self._count_dropped()
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
        self._publish()

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
                captured = True
            except Exception:
                captured = False
            errors = self._errors()
        try:
            if not captured:
                raise RuntimeError("the state cannot be captured")
            self._publish()
        except Exception:  # the state itself is broken: a minimal final frame still ends the run
            with self._lock:
                self._pending = []
            fallback = {"links": [], "heatmap": {}, "status": {
                "time_s": self._last_host or 0.0, "finished": True, "errors": errors,
                "messages": [f"run ended: the pipeline failed: {message}"]}}
            self._frames.append(json.dumps(fallback), final=True)

    def _connected_receivers(self) -> set[str]:
        return {name for name, s in self._ms.status().items() if _receiver_state(s) == "connected"}

    def _count_dropped(self) -> None:
        """Round broadcasts the sources' queue dropped (the pump fell behind) go into the ingest's counters."""
        dropped = self._ms.take_dropped()
        if dropped:
            with self._lock:
                for source, n in dropped.items():
                    self._state.count_dropped(source, n)

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
        self._publish()

    def _push_packet(self, receiver: str, pkt: Union[CSIPacket, HeardBroadcast]) -> None:
        """Caller holds the lock."""
        if isinstance(pkt, HeardBroadcast):
            self._state.push_broadcast(pkt.host_ts, pkt.data, receiver)
        else:
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
        self._publish()

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
        """Capture a frame at host time ``now``; ``_publish`` renders it. Caller holds the lock (the pump thread)."""
        self._pending.append((self._state.capture(now), self._status_inputs(now), final))

    def _publish(self) -> None:
        """Render the captured frames and append them to the log, without the lock. Only the pump thread emits, so
        frames keep their order."""
        with self._lock:
            pending, self._pending = self._pending, []
        for cap, inputs, final in pending:
            text = json.dumps(self._render(cap, inputs, links=self._links_in_frames))
            self._frames.append(text, final=final)
            if self._log_file is not None:
                self._log_file.write(text + "\n")
                self._log_file.flush()

    # ------------------------------------------------------------------ payload

    def _render(self, cap: Capture, inputs: dict, *, links: bool) -> dict:
        """A payload from a capture and its status inputs: no lock needed."""
        payload = self._state.render(cap, links=links)
        payload["status"]["broadcasts"] = {**payload["status"]["broadcasts"], "gateways": inputs["gateways"]}
        payload["status"].update(self._status_extension(inputs, cap.rows, payload["status"]["broadcasts"]))
        payload["status"]["messages"].extend(raw_messages(payload.get("raw_mode") or {}))
        payload["status"]["links_in_frames"] = self._links_in_frames
        return payload

    def _status_inputs(self, now: float) -> dict:
        """What the status extension needs from live state, copied. Caller holds the lock."""
        return {"receivers": {name: _receiver_state(s) for name, s in self._ms.status().items()},
                "errors": self._errors(), "finished": self._run_done, "calibrated": sorted(self._state.thresholds),
                "calibration": self._calib_status(now),
                "skipped": dict(self._calib.skipped) if self._calib is not None else {},
                "commands": list(self._commands), "commands_status": self._commands_status(),
                "gateways": {name: obj.losses() for name, obj in self._lossy.items()}}

    def _status_extension(self, inputs: Mapping, links: Sequence[Mapping], broadcasts: Mapping) -> dict:
        """The ``status`` extension from ``_status_inputs`` and the link rows (LinkMonitor.snapshot rows)."""
        errors = inputs["errors"]
        calibrated = inputs["calibrated"]
        adaptive = sorted(row["id"] for row in links if row.get("mode") == "auto")  # no threshold needed
        uncalibrated = sorted(self._labels - set(calibrated) - set(adaptive))
        silent = sorted(row["id"] for row in links if row["id"] in uncalibrated and row["age_s"] is None)
        heard = [label for label in uncalibrated if label not in silent]
        messages = list(self._notes) + broadcast_messages(broadcasts)
        if heard:
            messages.append(f"{len(heard)} link(s) UNCALIBRATED ({', '.join(heard)}): no threshold. "
                            "Pass --thresholds links.json, or calibrate on an empty room first.")
        if silent:  # a link that never sent a packet has nothing to calibrate: say so, not UNCALIBRATED
            where = "in this recording" if self._replay else "yet"
            messages.append(f"{len(silent)} link(s) have no data {where} ({', '.join(silent)}).")
        messages.extend(f"{'pipeline' if name == 'pipeline' else f'receiver {name}'} failed: {msg}"
                        for name, msg in sorted(errors.items()))
        messages.extend(f"calibration skipped {label}: {why}" for label, why in inputs["skipped"].items())
        messages.extend(f"command: {line}" for line in inputs["commands"])
        return {
            "receivers": inputs["receivers"],
            "errors": errors,
            "finished": inputs["finished"],
            "calibrated_links": calibrated,
            "adaptive_links": adaptive,
            "uncalibrated_links": uncalibrated,
            "messages": messages,
            "calibration": inputs["calibration"],
            "commands": inputs["commands_status"],
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
        """GET /api/state: the payload with every link row, captured under the lock and rendered without it."""
        with self._lock:
            now = self._clock()
            now = 0.0 if now is None else now
            cap, inputs = self._state.capture(now), self._status_inputs(now)
        return json.dumps(self._render(cap, inputs, links=True))

    # ------------------------------------------------------------------ commands

    def link_detail(self, label: Optional[str]) -> tuple[int, dict]:
        if not label:
            return 400, {"error": "name the link: /api/link?id=<link label>, for example id=n1%3C-n5 (n1<-n5)"}
        with self._lock:
            now = self._clock()
            row = self._state.link_detail(label, now)
        if row is None:
            return 404, {"error": f"unknown link {label!r}"}
        return 200, {"link": row, "time_s": now}

    def view(self, path: str) -> Optional[dict]:
        if path != "/api/thresholds" or self._replay:

            return None
        with self._lock:
            return {"thresholds": self._state.thresholds, "path": str(self._thresholds_path.resolve())}

    def command(self, path: str, body: Mapping) -> tuple[int, dict]:
        """Run one POST command under the lock. Returns (HTTP status, JSON object).

        A handler may return a callable instead: the part that writes to a gateway's port, which runs after the
        lock is released (ADR-0016), so a slow or wedged port never stalls the pump or /api/state."""
        outcome = self._command_locked(path, body)
        return outcome() if callable(outcome) else outcome

    def _command_locked(self, path: str, body: Mapping):
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
                "/api/raw-mode": self._cmd_raw_mode,
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
        try:
            self._state.set_threshold(link, float(value))
        except ValueError as e:   # a transceiver link takes no threshold: adaptive baseline only (ADR-0029)
            raise RequestError(str(e)) from None
        self._commands.append(f"threshold {link} = {float(value):.4g}")
        return 200, {"link": link, "threshold": float(value)}

    def _cmd_save(self, body: Mapping) -> tuple[int, dict]:
        thresholds = self._state.thresholds
        if not thresholds:
            return 409, {"error": "no thresholds to save: calibrate or set a link threshold first"}
        path = self._thresholds_path.resolve()
        try:
            save_thresholds(path, thresholds)
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
        try:
            self._state.set_params(**changes)
        except ValueError as e:   # e.g. the footprint falloff on a radio-tomography heatmap
            raise RequestError(str(e)) from None
        self._commands.append("parameters: " + ", ".join(f"{k}={float(v):g}" for k, v in body.items()))
        return 200, {"parameters": self._state.parameters()}

    def _cmd_raw_mode(self, body: Mapping) -> tuple[int, dict]:
        unknown = sorted(set(body) - {"link", "on"})
        if unknown:
            raise RequestError(f"unknown field(s): {', '.join(unknown)}; expected link and on")
        link, on = body.get("link"), body.get("on")
        if not isinstance(link, str) or not isinstance(on, bool):
            raise RequestError(f'name a link and true or false: {{"link": "n1<-n5", "on": true}}, got {dict(body)!r}')
        via = self._gateway_for(link)
        serial = getattr(self._gateways.get(via), "gateway_serial", None) if via else None
        now = self._clock()
        try:
            frames = self._state.raw_mode_request(link, on, 0.0 if now is None else now, serial)
        except RawModeRefused as e:
            return (400 if e.reason == "unknown_link" else 409), {"error": str(e), "reason": e.reason}
        gateway = self._gateways[via]

        def send() -> tuple[int, dict]:   # off the lock: a port write may block
            error = send_with_timeout(gateway, frames, SEND_TIMEOUT_S)
            if error is not None:
                reason, why = error
                return 503, {"error": f"gateway {via} could not send the raw mode request: {why}", "reason": reason}
            with self._lock:
                self._commands.append(f"raw mode {'on' if on else 'off'} {link} (sent via {via}, {len(frames)} "
                                      "copies)")
                status = self._state.raw_mode_status(0.0 if now is None else now)
            return 200, {"raw_mode": status, "sent": len(frames), "via": via}

        return send

    def _gateway_for(self, label: str) -> Optional[str]:
        """The source to send a raw mode request through: the one that last delivered the target node's round
        broadcast if it can send, else the first that knows its own serial."""
        last = self._state.last_source(label)
        if last in self._gateways:
            return last
        ready = [name for name, t in self._gateways.items() if getattr(t, "gateway_serial", None)]
        return ready[0] if ready else next(iter(self._gateways), None)

    def _apply_threshold(self, label: str, value: float) -> None:
        """Calibration's apply hook; the caller holds the lock."""
        self._state.set_threshold(label, value)
        self._commands.append(f"threshold {label} = {value:.4g} (calibration)")


def send_with_timeout(gateway: SendingGateway, frames: Sequence[bytes],
                      timeout_s: float) -> Optional[tuple[str, str]]:
    """Hand ``frames`` to ``gateway`` on a worker thread and wait at most ``timeout_s`` for it. None when every
    frame was written; else (reason, message): "send" for a port error, "timeout" for a write still blocked (the
    worker is left to finish or fail on its own; it is a daemon, so it never holds the process up)."""
    result: list[Optional[tuple[str, str]]] = []

    def write() -> None:
        try:
            for frame in frames:
                gateway.send_frame(frame)
            result.append(None)
        except (OSError, RuntimeError) as e:
            result.append(("send", str(e) or type(e).__name__))

    worker = threading.Thread(target=write, name="gateway-send", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if not result:
        return "timeout", f"the port write did not finish within {timeout_s:g} s"
    return result[0]


def broadcast_messages(counts: Mapping) -> list[str]:
    """Operator lines for round broadcasts that were not used: unknown nodes, rejections, queue overflow."""
    out = []
    unknown = counts.get("unknown_nodes") or {}
    if unknown:
        named = ", ".join(f"{serial} ({n})" for serial, n in sorted(unknown.items()))
        out.append(f"{len(unknown)} unknown node(s) heard, not in config/room.json: {named}. Their round "
                   "broadcasts are counted but not used: add each serial to the room config, or switch the board off.")
    rejected = counts.get("rejected") or {}
    bad = {reason: n for reason, n in rejected.items() if n and reason not in (DUPLICATE, QUEUE_OVERFLOW)}
    if bad:
        hint = (" Failed authentication (auth) means a node has another kit key (compare `scripts/node.py PORT key` "
                "with the host's) or someone is sending forged round broadcasts." if bad.get(AUTH) else "")
        out.append(f"{sum(bad.values())} round broadcast(s) rejected: "
                   + ", ".join(f"{reason} {n}" for reason, n in sorted(bad.items())) + "." + hint)
    if rejected.get(QUEUE_OVERFLOW):
        out.append(f"{rejected[QUEUE_OVERFLOW]} round broadcast(s) dropped because the host fell behind (queue full).")
    for name, lost in sorted((counts.get("gateways") or {}).items()):
        parts = {key: lost.get(key, 0) for key in LOSS_COUNTERS}
        parts.update(lost.get("records_rejected") or {})
        parts = {key: n for key, n in parts.items() if n}
        if parts:
            out.append(f"gateway {name} lost frames before the host: " + ", ".join(f"{k} {n}" for k, n in parts.items())
                       + " (queue_full, usb_dropped and csi_skipped on the gateway since its boot; the rest are USB "
                       "records that failed their checks).")
    return out


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


def simulated_rig_streams(room: Room, *, seed: int = 0, quiet_first_s: float = 0.0,
                          realtime: bool = True) -> dict[str, Stream]:
    """One endless round broadcast stream from the simulated transceiver rig, as one gateway ("sim-gateway") hears
    it: ``default_walk`` repeated, after ``quiet_first_s`` seconds of an empty room. The stream is a
    ``SimulatedRig``, so it is also the gateway that sends raw mode requests."""
    return {"sim-gateway": SimulatedRig(room, default_walk(room), quiet_first_s=quiet_first_s, seed=seed,
                                        realtime=realtime)}
