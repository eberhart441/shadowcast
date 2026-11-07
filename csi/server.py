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
import itertools
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, Optional
from urllib.parse import urlsplit

from .dashboard import DashboardState
from .links import LinkId, calibrate_links
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
LIVE_LAG_FRAMES = 5        # a live client further behind than this skips to the newest frame
KEEPALIVE_S = 15.0         # SSE comment sent when idle, so dead clients are noticed
WAIT_TICK_S = 0.2          # how often idle handlers re-check for shutdown
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SIM_WALK_SPEED_MPS = 0.5
SIM_CALIBRATION_S = 25.0
SIM_CALIBRATION_SEED = 10


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
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

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


class DashboardServer:
    def __init__(self, room: Room, sources: Mapping[str, Stream], thresholds: Optional[Mapping[str, float]] = None,
                 *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 frame_interval_s: float = FRAME_INTERVAL_S, web_index: Optional[Path] = None,
                 **heatmap_params) -> None:
        self._room = room
        self._state = DashboardState(room, dict(thresholds or {}), **heatmap_params)
        self._labels = {LinkId(rx.name, tx.mac).label for rx, tx in room.links()}
        self._calibrated = {label for label in self._labels if thresholds and label in thresholds}
        self._ms = MultiSource(sources)
        self._lock = threading.Lock()
        self._frames = FrameLog()
        self._frame_s = frame_interval_s
        self._next_frame_at: Optional[float] = None
        self._last_host: Optional[float] = None
        self._last_mono: Optional[float] = None
        self._run_done = False       # set under the lock, just before the final frame
        self._sources_done = threading.Event()
        self._stop = threading.Event()
        self._stopped = False
        self._web_index = WEB_DIR / "index.html" if web_index is None else Path(web_index)
        self._httpd = _Httpd((host, port), _Handler)
        self._httpd.dashboard = self
        self._pump_thread: Optional[threading.Thread] = None
        self._http_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{port}"

    def start(self) -> None:
        self._ms.start()
        self._pump_thread = threading.Thread(target=self._pump, name="dashboard-pump", daemon=True)
        self._pump_thread.start()
        self._http_thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                             name="dashboard-http", daemon=True)
        self._http_thread.start()

    def wait_finished(self, timeout: Optional[float] = None) -> bool:
        """True once every source has ended and the final frame is in the log."""
        return self._sources_done.wait(timeout)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._http_thread is not None:
            self._httpd.shutdown()
        self._httpd.server_close()
        self._ms.close()
        for thread in (self._pump_thread, self._http_thread):
            if thread is not None:
                thread.join(timeout=5.0)
        self._sources_done.set()

    def __enter__(self) -> "DashboardServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

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
                with self._lock:
                    self._run_done = True
                    self._emit(self._last_host or 0.0, final=True)
        finally:
            self._sources_done.set()

    def _connected_receivers(self) -> set[str]:
        return {name for name, s in self._ms.status().items() if _receiver_state(s) == "connected"}

    def _feed(self, batch: list[tuple[str, CSIPacket]]) -> None:
        if not batch:
            return
        with self._lock:
            for receiver, pkt in batch:
                self._state.push(receiver, pkt)
                if self._last_host is None or pkt.host_ts > self._last_host:
                    self._last_host = pkt.host_ts
                if self._next_frame_at is None:
                    self._next_frame_at = pkt.host_ts + self._frame_s
                elif pkt.host_ts >= self._next_frame_at:
                    self._emit(pkt.host_ts)
                    self._next_frame_at = pkt.host_ts + self._frame_s
            self._last_mono = time.monotonic()

    def _idle_frame(self) -> None:
        """With no packets arriving, keep frames coming on the clock so silent links turn NO_SIGNAL."""
        with self._lock:
            now = self._clock()
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
        self._frames.append(json.dumps(self._payload(now)), final=final)

    # ------------------------------------------------------------------ payload

    def _payload(self, now: float) -> dict:
        """Caller holds the lock: DashboardState advances its smoothing to ``now``."""
        payload = self._state.snapshot(now)
        payload["status"].update(self._status_extension())
        return payload

    def _status_extension(self) -> dict:
        receivers = {name: _receiver_state(s) for name, s in self._ms.status().items()}
        errors = self._ms.errors()
        uncalibrated = sorted(self._labels - self._calibrated)
        messages = []
        if uncalibrated:
            messages.append(f"{len(uncalibrated)} link(s) UNCALIBRATED ({', '.join(uncalibrated)}): no threshold. "
                            "Pass --thresholds links.json, or calibrate on an empty room first.")
        messages.extend(f"receiver {name} failed: {msg}" for name, msg in sorted(errors.items()))
        return {
            "receivers": receivers,
            "errors": errors,
            "finished": self._run_done,
            "calibrated_links": sorted(self._calibrated),
            "uncalibrated_links": uncalibrated,
            "messages": messages,
        }

    def state_json(self) -> str:
        with self._lock:
            now = self._clock()
            return json.dumps(self._payload(0.0 if now is None else now))

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


def looping_room_streams(room: Room, person: Walk, *, seed: int = 0) -> dict[str, Stream]:
    """Endless real-time streams, one per receiver, of a person repeating ``person``'s loop.

    Each cycle is ``simulate_room`` over one loop period with the same seed, so the cycles join smoothly.
    """
    period = walk_period_s(person)
    return {rx.name: _looping(rx.name, room, person, period, seed) for rx in room.receivers}


def _looping(name: str, room: Room, person: Walk, period: float, seed: int) -> Iterator[tuple[float, str]]:
    for cycle in itertools.count():
        lines = simulate_room(person, room=room, duration_s=period, seed=seed, realtime=True)[name]
        yield from (lines if cycle == 0 else itertools.islice(lines, 1, None))  # one header is enough
