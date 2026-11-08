"""The live pump survives a pipeline exception: the run is marked failed, a final frame is sent, and clients see it.

Driven through the HTTP interface. The bad packet comes from a source that yields parsed packets (the replay
seam), with an amplitude width the link's detector cannot accept.
"""
import dataclasses
import json
import urllib.request

import numpy as np
import pytest

from csi.parser import parse_stream
from csi.room import load_room
from csi.server import DashboardServer
from csi.simulate import simulate_room

ROOM = load_room()
T0 = 1_700_000_000.0


def breaking_source(n_good: int = 300):
    """Good packets of rx1 for a short empty room, then one packet whose amplitude width changes mid-link."""
    packets = list(parse_stream(simulate_room(None, room=ROOM, duration_s=5, seed=12, t0=T0)["rx1"]))
    good = packets[:n_good]
    # later in device time too: a repeated device time would be dropped as out of order before the pipeline sees it
    bad = dataclasses.replace(good[-1], host_ts=good[-1].host_ts + 0.01, device_ts=good[-1].device_ts + 10_000,
                              amplitude=np.ones(3, dtype=np.float32))
    for pkt in good + [bad]:
        yield pkt.host_ts, pkt


@pytest.fixture
def server():
    started = []

    def _serve(sources, **kwargs):
        s = DashboardServer(ROOM, sources, None, port=0, **kwargs)
        s.start()
        started.append(s)
        return s

    yield _serve
    for s in started:
        s.stop()


def get_json(server, path="/api/state"):
    with urllib.request.urlopen(server.url + path, timeout=30) as resp:
        return json.loads(resp.read())


def sse_payloads(server):
    """Every payload the event stream sends for a finished run, up to and including the final one."""
    out = []
    with urllib.request.urlopen(server.url + "/api/events", timeout=30) as resp:
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
                if out[-1]["status"]["finished"]:
                    break
    return out


def test_a_pipeline_exception_ends_the_run_with_the_error_and_a_final_frame(server):
    s = server({"rx1": breaking_source()})
    assert s.wait_finished(timeout=30), "the run must finish even though the pipeline raised"

    payload = get_json(s)

    assert payload["status"]["finished"] is True
    assert payload["status"]["receivers"]["rx1"] == "failed"
    assert payload["status"]["errors"]["rx1"]           # the error text is surfaced
    assert "rx1" in " ".join(payload["status"]["messages"]) and "failed" in " ".join(payload["status"]["messages"])


def test_clients_connecting_after_the_failure_get_the_final_frame_not_a_stale_picture(server):
    s = server({"rx1": breaking_source()})
    assert s.wait_finished(timeout=30)

    events = sse_payloads(s)

    assert events and events[-1]["status"]["finished"] is True
    assert events[-1]["status"]["errors"]
