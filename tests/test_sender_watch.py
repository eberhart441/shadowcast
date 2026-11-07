"""Single-link tools (detect_live, telemetry) warn once when a stream carries more than one sender MAC."""
import heapq
from operator import itemgetter

from csi.common import SenderWatch
from csi.parser import parse_stream
from csi.simulate import Segment, simulate_stream

MAC_A, MAC_B = "1a:00:00:00:00:00", "1a:00:00:00:00:01"


def run(stream):
    messages = []
    watch = SenderWatch(messages.append)
    packets = list(watch.observe_all(parse_stream(stream)))
    return messages, packets


def test_two_senders_on_one_stream_warn_once_and_name_both_and_point_at_links_live():
    a = simulate_stream(scenario=[Segment("empty", 2)], seed=1, mac=MAC_A)
    b = simulate_stream(scenario=[Segment("empty", 2)], seed=2, mac=MAC_B)
    messages, packets = run(heapq.merge(a, b, key=itemgetter(0)))

    assert len(messages) == 1
    assert MAC_A in messages[0] and MAC_B in messages[0]
    assert "links_live.py" in messages[0]
    assert packets                                # the packets still pass through


def test_one_sender_or_unlabelled_packets_do_not_warn():
    messages, _ = run(simulate_stream(scenario=[Segment("empty", 2)], seed=1, mac=MAC_A))
    assert messages == []
    messages, _ = run(simulate_stream(scenario=[Segment("empty", 2)], seed=1, mac=""))
    assert messages == []
