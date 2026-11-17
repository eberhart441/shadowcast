"""A staged rollout run by the dashboard (POST /api/ota) beside the live pipeline, on its own thread.
"""
from __future__ import annotations

import time

from .server import ImageServer

JOB_POLL_S = 0.05
JOB_LOG_KEPT = 50


class RolloutJob:
    """A staged rollout the dashboard runs (POST /api/ota) beside the live pipeline, on its own thread.

    It owns the image server and the ``Rollout``. ``heard(serial)`` is called for every round broadcast the
    dashboard accepts (cheap: it queues); the job's thread feeds the queued heard serials and the image server's
    reports to the rollout, steps it, and hands the maintenance commands to ``send`` (a gateway's port write)
    without holding the job's lock, so a slow port never stalls ``status`` or the dashboard. The image server
    closes when the rollout ends or ``stop`` is called.
    """

    def __init__(self, make_rollout, server: "ImageServer", send, *, clock=time.monotonic,
                 poll_s: float = JOB_POLL_S) -> None:
        import threading
        from collections import deque

        self._lock = threading.Lock()
        self._outbox: deque = deque()
        self._heard: deque = deque()
        self.messages: deque = deque(maxlen=JOB_LOG_KEPT)
        self._rollout = make_rollout(send=self._outbox.append, log=self.messages.append)
        self._server, self._send, self._clock, self._poll = server, send, clock, poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ota-rollout", daemon=True)
        self.send_error = ""

    def start(self) -> "RolloutJob":
        self._server.start()
        self._thread.start()
        return self

    def heard(self, serial: str) -> None:
        self._heard.append((serial, self._clock()))

    @property
    def active(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                now = self._clock()
                with self._lock:
                    for r in self._server.take_reports():
                        self._rollout.report(r, now)
                    while self._heard:
                        serial, at = self._heard.popleft()
                        self._rollout.heard(serial, at)
                    state = self._rollout.step(now)
                while self._outbox:
                    try:
                        self._send(self._outbox.popleft())
                    except (OSError, RuntimeError) as e:
                        self.send_error = str(e) or type(e).__name__
                        self.messages.append(f"the gateway could not send a maintenance command: {self.send_error}")
                if state != "running":
                    return
                self._stop.wait(self._poll)
        finally:
            self._server.close()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def status(self) -> dict:
        """``status.ota`` in the payload: state (running, done, failed or stopped), stage of stages, the failure, the
        outcome each node reported, the image server's port and the rollout's log lines."""
        with self._lock:
            r = self._rollout
            state = r.state if r.state != "running" or self._thread.is_alive() else "stopped"
            return {"state": state, "stage": r.stage, "stages": r.stage_count, "failure": r.failure,
                    "results": dict(r.results), "image_port": self._server.port, "log": list(self.messages)}
