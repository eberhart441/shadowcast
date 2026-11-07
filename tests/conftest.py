"""Pin the room layout used by tests to a frozen four-node fixture.

The real rig is described by the committed room config and changes as boards are added or moved. Tests
that build on "the default room" must not depend on it, so csi.room.DEFAULT_ROOM_PATH (and the
--room default of the scripts, also run as subprocesses) is pointed at a frozen copy via CSI_ROOM
before any test module imports csi.
"""
import os
from pathlib import Path

import pytest

FROZEN_ROOM = Path(__file__).resolve().parent / "fixtures" / "room_rig4.json"
os.environ["CSI_ROOM"] = str(FROZEN_ROOM)


@pytest.fixture(autouse=True)
def _no_sse_pacing(request, monkeypatch):
    """The event stream sends at most ~5 events per wall-clock second, so reading a 15 s run takes 15 s.

    That pace is for browsers; the tests read the same events without it. A test about the pace opts out with
    ``@pytest.mark.real_sse_pacing``.
    """
    if request.node.get_closest_marker("real_sse_pacing") is None:
        import csi.server

        monkeypatch.setattr(csi.server, "MIN_EVENT_GAP_S", 0.0)
