"""The accuracy run's building blocks: raw recording (csi/rawlog.py) and ground truth from prompts (csi/accuracy.py)."""
import json

import pytest

from csi import accuracy as A
from csi.rawlog import read_raw, tee_sources


def test_tee_passes_lines_through_and_records_them_with_their_receiver(tmp_path):
    path = tmp_path / "raw.tsv"
    sources = {"rx1": iter([(1.0, "CSI_DATA,a\n"), (1.5, None), (2.0, "CSI_DATA,b")]),
               "rx2": iter([(1.2, "CSI_DATA,c")])}
    teed = tee_sources(sources, path)
    assert list(teed["rx1"]) == [(1.0, "CSI_DATA,a\n"), (1.5, None), (2.0, "CSI_DATA,b")]
    assert list(teed["rx2"]) == [(1.2, "CSI_DATA,c")]
    assert read_raw(path) == {"rx1": [(1.0, "CSI_DATA,a"), (2.0, "CSI_DATA,b")], "rx2": [(1.2, "CSI_DATA,c")]}


def test_read_raw_keeps_only_the_asked_time_window(tmp_path):
    path = tmp_path / "raw.tsv"
    path.write_text("1.0\trx1\tA\n2.0\trx1\tB\nbroken line\n3.0\trx1\tC\n")
    assert read_raw(path, 1.5, 3.0) == {"rx1": [(2.0, "B")]}


def test_marks_come_in_mirror_twin_pairs_through_the_room_centre():
    for k, (x, y) in A.MARKS.items():
        tx, ty = A.MARKS[A.twin(k)]
        assert (x + tx, y + ty) == pytest.approx((2.77, 1.84), abs=0.01)


def labels(*events):
    return [dict(e) for e in events]


def test_hold_segments_skip_the_settling_time_and_carry_the_mark():
    segs = A.segments_from_labels(labels({"t": 100.0, "event": "hold", "mark": 3},
                                         {"t": 120.0, "event": "hold_end", "mark": 3}), offset=10.0)
    (s,) = segs
    assert (s.kind, s.mark) == ("hold", 3)
    assert s.t0 == pytest.approx(90.0 + A.HOLD_SETTLE_S) and s.t1 == pytest.approx(110.0 - 0.5)
    assert s.truth(95.0) == A.MARKS[3]


def test_walk_truth_moves_in_straight_lines_between_called_marks():
    segs = A.segments_from_labels(labels({"t": 0.0, "event": "walk_call", "mark": 2, "index": 0},
                                         {"t": 3.0, "event": "walk_call", "mark": 3, "index": 1},
                                         {"t": 6.0, "event": "walk_end"}))
    (walk,) = segs
    (x1, y1), (x2, y2), (x3, y3) = A.MARKS[1], A.MARKS[2], A.MARKS[3]
    assert walk.truth(0.0) == pytest.approx((x1, y1))
    assert walk.truth(1.5) == pytest.approx(((x1 + x2) / 2, (y1 + y2) / 2))
    assert walk.truth(3.0) == pytest.approx((x2, y2))
    assert walk.truth(5.999) == pytest.approx((x3, y3), abs=1e-3)


def test_the_final_empty_room_waits_longer_for_the_person_to_leave():
    segs = A.segments_from_labels(labels({"t": 0.0, "event": "empty_start"}, {"t": 60.0, "event": "empty_end"},
                                         {"t": 100.0, "event": "empty_start", "final": True},
                                         {"t": 130.0, "event": "empty_end", "final": True}))
    first, final = segs
    assert first.t0 == pytest.approx(A.EMPTY_SETTLE_S) and not first.meta["final"]
    assert final.t0 == pytest.approx(100.0 + A.LEAVE_SETTLE_S) and final.meta["final"]


def test_labels_round_trip_through_json():
    e = {"t": 1.5, "event": "goto", "mark": 4}
    assert A.segments_from_labels([json.loads(json.dumps(e))]) == []
