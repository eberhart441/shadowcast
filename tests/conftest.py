"""Pin the room layout used by tests to a frozen four-node fixture.

The real rig is described by config/room.json and changes as boards are added or moved. Tests
that build on "the default room" must not depend on it, so csi.room.DEFAULT_ROOM_PATH (and the
--room default of the scripts, also run as subprocesses) is pointed at a frozen copy via CSI_ROOM
before any test module imports csi.
"""
import os
from pathlib import Path

FROZEN_ROOM = Path(__file__).resolve().parent / "fixtures" / "room_rig4.json"
os.environ["CSI_ROOM"] = str(FROZEN_ROOM)
