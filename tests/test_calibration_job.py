"""The live calibration job's packet intake: packets before the recording start, and packets in the pauses between
clips, must not land in a clip. Driven through CalibrationJob's own interface (observe / tick / status)."""
from dataclasses import replace

from csi.live_calibration import CalibrationJob, parse_request
from csi.room import load_room
from csi.simulate import simulate_room_links

ROOM = load_room()


def run_job(packets, t_start: float, stale=()):
    """Feed one calibration run; ``stale`` packets are fed right after the recording starts."""
    job = CalibrationJob(parse_request({"clips": 2, "seconds": 5, "lead_s": 0}), expected=[], now_mono=0.0,
                         current=lambda: {}, apply=lambda label, value: None)
    job.tick(t_start, 0.0)                       # countdown over: recording starts at t_start
    for receiver, pkt in stale:
        job.observe(receiver, pkt, 0.0)
    for receiver, pkt in packets:
        job.observe(receiver, pkt, 0.0)
    status = job.status(None, 0.0)
    return job.state, status["result"], status["skipped"]


def test_packets_before_the_recording_start_are_ignored_and_do_not_change_the_result():
    packets = list(simulate_room_links(room=ROOM, person=None, duration_s=20, seed=12))
    t_start = packets[0][1].host_ts
    # the same packets, plus stale ones stamped before the start (a late packet from another receiver)
    stale = [(rx, replace(pkt, host_ts=t_start - offset)) for offset in (0.5, 4.0, 9.5)
             for rx, pkt in packets[:200]]

    clean = run_job(packets, t_start)
    polluted = run_job(packets, t_start, stale=stale)

    assert clean[0] == "done", clean
    assert polluted == clean


def test_packets_in_the_pause_between_clips_are_ignored_and_do_not_change_the_result():
    packets = list(simulate_room_links(room=ROOM, person=None, duration_s=20, seed=12))
    t_start = packets[0][1].host_ts
    # clip 1 covers [0, 5) s, the pause [5, 8) s; stamp extra packets inside the pause
    pause = [(rx, replace(pkt, host_ts=t_start + 5.5 + i * 0.001)) for i, (rx, pkt) in enumerate(packets[:500])]
    in_order = sorted(packets + pause, key=lambda item: item[1].host_ts)

    clean = run_job(packets, t_start)
    polluted = run_job(in_order, t_start)

    assert polluted == clean
