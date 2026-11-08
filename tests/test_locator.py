"""Locator (csi/locator.py): presence and position from each link's raw CSI, with its uncertainty."""
import numpy as np
import pytest

import csi.locator as L
from csi.room import Stand, load_room
from csi.simulate import simulate_room_links

ROOM = load_room()
T0 = 1_700_000_000.0


def packets(person, seconds, t0, seed=1):
    """The simulator's seed fixes each subcarrier's level, so one seed is one set of physical links: keep it."""
    return list(simulate_room_links(person=person, room=ROOM, duration_s=seconds, rate_hz=38, seed=seed, t0=t0))


def calibrated_locator():
    loc = L.Locator(ROOM)
    assert loc.reference_from_clips([packets(None, 30, T0)])
    return loc


def feed(loc, pkts):
    for receiver, pkt in pkts:
        loc.push(receiver, pkt)
        loc.step(pkt.host_ts)
    return loc.payload()


def test_without_a_reference_the_locator_says_so():
    loc = L.Locator(ROOM)
    assert feed(loc, packets(None, 3, T0))["state"] == "uncalibrated"


def test_an_empty_room_stays_empty():
    loc = calibrated_locator()
    out = feed(loc, packets(None, 20, T0 + 100))
    assert out["state"] == "empty" and not out["present"]


def test_a_person_moving_near_the_left_wall_is_present_on_the_left():
    loc = calibrated_locator()
    feed(loc, packets(None, 5, T0 + 100))
    out = feed(loc, packets(Stand(0.3, 0.9), 8, T0 + 105))
    assert out["state"] == "moving" and out["present"]
    assert out["x"] < ROOM.width_m / 2
    assert out["sigma"]["major_m"] >= out["sigma"]["minor_m"] >= L.MIN_SIGMA_M


def test_after_motion_stops_the_state_is_maybe_still_then_empty():
    loc = calibrated_locator()
    feed(loc, packets(Stand(0.3, 0.9), 8, T0 + 100))
    out = feed(loc, packets(None, 10, T0 + 108))
    assert out["state"] == "maybe_still" and "x" in out          # still shown where it was last seen
    out = feed(loc, packets(None, L.STILL_HOLD_S + 5, T0 + 118))
    assert out["state"] == "empty" and "x" not in out


def test_the_reported_uncertainty_is_never_smaller_than_the_measured_error_there():
    loc = calibrated_locator()
    loc.set_accuracy_map({"marks": {"1": {"truth": [0.46, 0.4], "err_p68_m": 0.9}},
                          "error_given_estimate": {"p68_m": [[1.2] * 4] * 3}, "note": "test"})
    out = feed(loc, packets(Stand(0.3, 0.9), 8, T0 + 100))
    assert out["measured_error_m"] == pytest.approx(1.2)
    assert out["sigma"]["minor_m"] >= 1.2
    assert out["accuracy_note"] == "test"


def test_the_reference_survives_a_save_and_load(tmp_path):
    loc = calibrated_locator()
    loc.save(tmp_path / "ref.json")
    other = L.Locator(ROOM)
    import json
    other.load(json.loads((tmp_path / "ref.json").read_text()))
    assert other.calibrated
    for label in loc._ref:
        np.testing.assert_allclose(other._ref[label], loc._ref[label], rtol=1e-4)
