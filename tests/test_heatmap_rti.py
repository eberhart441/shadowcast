"""The transceiver heatmap: every link's evidence imaged over the room by regularised radio tomography.

Driven through the transceiver seam (simulated round broadcasts into DashboardState) for accuracy and through
Heatmap.advance (the step the dashboard runs every 0.1 s of host time) for the per-frame cost. Rooms: the frozen
8-node and 30-node rooms (ADR-0017), both 4 x 3 m, so more nodes means more links over the same floor.
"""
import time
from pathlib import Path

import numpy as np
import pytest

from csi.heatmap import Heatmap
from csi.links import room_link_ids
from csi.room import load_room

FIXTURES = Path(__file__).parent / "fixtures"
ROOM8 = load_room(FIXTURES / "room_tr8.json")
ROOM30 = load_room(FIXTURES / "room_tr30.json")

# Per-frame budget at 30 nodes (870 links): one heatmap step (every link's baseline and evidence, then the image),
# with a full 60 s quiet baseline behind every link, must take at most a tenth of the dashboard's 0.2 s frame
# interval. The heatmap steps every 0.1 s of host time and once more per frame, so it then uses at most about a
# third of the frame interval in the worst case; about 6 ms per step was measured on the development PC.
HEATMAP_STEP_BUDGET_S = 0.020


@pytest.mark.slow
def test_one_heatmap_step_at_30_nodes_fits_the_per_frame_budget():
    """Measured as CPU time of this process over the last 100 steps, so other processes on a busy machine (parallel
    test workers) do not count against it."""
    labels = [link_id.label for link_id, _, _ in room_link_ids(ROOM30)]
    assert len(labels) == 870
    heat = Heatmap(ROOM30)
    rng = np.random.default_rng(0)
    steps, timed = 700, 100                        # 70 s at the 0.1 s step: the 60 s baselines are full
    frames = [[{"id": label, "state": "EMPTY", "score": float(s), "age_s": 0.0}
               for label, s in zip(labels, rng.lognormal(0.0, 0.2, len(labels)))] for _ in range(timed)]
    for step in range(steps - timed):
        heat.advance(step * 0.1, frames[step % timed])
    started = time.process_time()
    for step in range(steps - timed, steps):
        heat.advance(step * 0.1, frames[step % timed])
    per_step = (time.process_time() - started) / timed
    assert per_step <= HEATMAP_STEP_BUDGET_S
