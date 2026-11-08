"""Out-of-order packets at the packet seam: a late packet is dropped and counted, and the scores that follow are not
distorted compared with the in-order stream."""
import pytest

from csi.detect import MotionScorer
from csi.parser import parse_stream
from csi.simulate import Segment, simulate_stream

SWAP_AT = 200


@pytest.fixture(scope="module")
def packets():
    return list(parse_stream(simulate_stream(scenario=[Segment("motion", 6)], seed=5)))


def swapped(pkts, i):
    """The same packets with the pair at i and i+1 arriving in reverse order."""
    return pkts[:i] + [pkts[i + 1], pkts[i]] + pkts[i + 2:]


def scores_and_scorer(pkts):
    scorer = MotionScorer()
    return [scorer.push(p) for p in pkts], scorer


def test_a_late_packet_is_dropped_and_counted_not_applied_as_a_time_step(packets):
    _, scorer = scores_and_scorer(swapped(packets, SWAP_AT))

    assert scorer.dropped == 1


def test_scores_after_a_swapped_pair_match_the_in_order_stream(packets):
    in_order, _ = scores_and_scorer(packets)
    with_swap, _ = scores_and_scorer(swapped(packets, SWAP_AT))

    for j in range(SWAP_AT + 2, SWAP_AT + 150):
        assert in_order[j] is not None and with_swap[j] is not None
        assert abs(with_swap[j] - in_order[j]) <= 0.005 * in_order[j], j


def test_in_order_stream_drops_nothing(packets):
    _, scorer = scores_and_scorer(packets)

    assert scorer.dropped == 0
