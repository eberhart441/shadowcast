"""The staged rollout (csi/ota/rollout.py Rollout): which nodes get the command when, and when a rollout stops.

Driven with a fake clock: the test plays the nodes, answering with reports (what the image server receives) and
round broadcasts heard again (what the gateway hears). The commands it sends are read back with decode_command.
"""
import pytest

from csi import ota
from csi.round_auth import TEST_KIT_KEY, tag_ok

SERIALS = [f"02:00:00:00:{i:02X}:5C" for i in range(1, 31)]
IMAGE = ota.ImageInfo(size=0x0E1000, sha256=bytes(range(32)), signer=bytes(32), chip_id=23, version="v2")


def test_stages_grow_from_two_to_six_to_the_rest_in_commands_of_at_most_13():
    stages = ota.plan_stages(SERIALS)
    assert [len(s) for s in stages] == [2, 6, 13, 9]
    assert sum(stages, []) == SERIALS
    assert ota.plan_stages(SERIALS[:2]) == [SERIALS[:2]]
    assert [len(s) for s in ota.plan_stages(SERIALS[:5])] == [2, 3]
    assert ota.plan_stages(SERIALS[:3], sizes=(1,)) == [SERIALS[:1], SERIALS[1:3]]


class Rig:
    """The fake world: a clock, the frames the gateway was told to send, the rollout under test."""

    def __init__(self, stages, **kwargs):
        self.t = 100.0
        self.sent = []
        self.rollout = ota.Rollout(stages, TEST_KIT_KEY, image=IMAGE, port=8070, ap_channel=36, sensing_channel=6,
                                   send=self.sent.append, wall=lambda: 1794000000 + self.t, log=lambda m: None,
                                   **kwargs)

    def run(self, seconds, step=0.1):
        end = self.t + seconds
        while self.t < end and self.rollout.state == "running":
            self.rollout.step(self.t)
            self.t += step
        return self.rollout.state

    def commands(self):
        return [ota.decode_command(b) for b in self.sent]

    def answer(self, serial, outcome="committed", issued=None):
        issued = self.commands()[-1].issued if issued is None else issued
        self.rollout.report(ota.Report(serial, issued, outcome, "ota_1", "0001020304050607", "v2"), self.t)

    def rejoin(self, serial):
        self.rollout.heard(serial, self.t)


def test_each_stage_is_commanded_three_times_and_the_next_waits_for_every_node():
    rig = Rig([SERIALS[:2], SERIALS[2:4]])
    rig.run(1.0)
    first = rig.commands()
    assert len(first) == 3 and all(c.targets == tuple(SERIALS[:2]) for c in first)
    assert len({bytes(b) for b in rig.sent}) == 1 and all(tag_ok(b, TEST_KIT_KEY) for b in rig.sent)
    c = first[0]
    assert (c.command, c.port, c.ap_channel, c.image_size, c.image_sha256) == (
        ota.OTA_CMD_UPDATE, 8070, 36, IMAGE.size, IMAGE.sha256)
    rig.answer(SERIALS[0])
    rig.rejoin(SERIALS[0])
    rig.run(30)
    assert len(rig.sent) == 3, "the next stage started before every node of this one came back"
    rig.answer(SERIALS[1])
    rig.run(5)
    assert len(rig.sent) == 3, "the next stage started before the last node was heard on the round again"
    rig.rejoin(SERIALS[1])
    rig.run(1)
    second = rig.commands()[3:]
    assert len(second) == 3 and second[0].targets == tuple(SERIALS[2:4])
    assert second[0].issued > first[0].issued
    for s in SERIALS[2:4]:
        rig.answer(s)
        rig.rejoin(s)
    assert rig.run(5) == "done"
    assert rig.rollout.results == {s: "committed" for s in SERIALS[:4]}


def test_a_node_heard_before_its_report_has_not_rejoined_yet():
    rig = Rig([SERIALS[:1]])
    rig.run(1)
    rig.rejoin(SERIALS[0])          # still the old image, before it left for maintenance mode
    rig.run(10)
    rig.answer(SERIALS[0])
    assert rig.run(5) == "running"
    rig.rejoin(SERIALS[0])
    assert rig.run(1) == "done"


@pytest.mark.parametrize("outcome", ["rolled_back", "refused_image", "refused_digest", "no_server"])
def test_a_bad_outcome_stops_the_rollout_before_the_next_stage(outcome):
    rig = Rig([SERIALS[:2], SERIALS[2:8]])
    rig.run(1)
    rig.answer(SERIALS[0], outcome)
    assert rig.run(400) == "failed"
    assert len(rig.sent) == 3
    assert SERIALS[0] in rig.rollout.failure and outcome in rig.rollout.failure


def test_a_node_that_never_reports_stops_the_rollout_at_the_timeout():
    rig = Rig([SERIALS[:2], SERIALS[2:8]], report_timeout_s=60)
    rig.run(1)
    rig.answer(SERIALS[0])
    rig.rejoin(SERIALS[0])
    assert rig.run(58) == "running"
    assert rig.run(5) == "failed"
    assert SERIALS[1] in rig.rollout.failure and "no report" in rig.rollout.failure
    assert len(rig.sent) == 3


def test_a_node_that_commits_but_never_rejoins_stops_the_rollout():
    rig = Rig([SERIALS[:1], SERIALS[1:2]], rejoin_timeout_s=30)
    rig.run(1)
    rig.answer(SERIALS[0])
    assert rig.run(29) == "running"
    assert rig.run(2) == "failed"
    assert "round" in rig.rollout.failure


def test_reports_for_another_command_or_node_are_ignored():
    rig = Rig([SERIALS[:1]])
    rig.run(1)
    rig.answer(SERIALS[0], issued=rig.commands()[0].issued - 60)   # an old command's report
    rig.answer(SERIALS[5])                                           # a node outside the stage
    rig.rejoin(SERIALS[0])
    assert rig.run(5) == "running"
    assert rig.rollout.results == {}


def test_already_running_the_image_counts_as_done():
    rig = Rig([SERIALS[:1]])
    rig.run(1)
    rig.answer(SERIALS[0], "already")
    rig.rejoin(SERIALS[0])
    assert rig.run(2) == "done"


def test_a_check_in_needs_no_image():
    sent = []
    rollout = ota.Rollout([SERIALS[:1]], TEST_KIT_KEY, command=ota.OTA_CMD_REPORT, image=None, port=8070,
                          ap_channel=36, sensing_channel=6, send=sent.append, log=lambda m: None)
    rollout.step(0.0)
    assert ota.decode_command(sent[0]).command == ota.OTA_CMD_REPORT


def test_a_rollout_refuses_a_network_on_the_sensing_channel():
    with pytest.raises(ValueError, match="overlaps"):
        Rig([SERIALS[:1]]).rollout.__class__([SERIALS[:1]], TEST_KIT_KEY, image=IMAGE, port=8070, ap_channel=5,
                                             sensing_channel=6, send=print)
