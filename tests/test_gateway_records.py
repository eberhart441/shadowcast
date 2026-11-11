"""The gateway source: a node gateway's USB byte stream in, (host time, round broadcast bytes) out (ADR-0022).

The byte stream is faked: records built from the golden fixtures (tests/fixtures/gateway_record_v1.json, the same
bytes the node's encoder is tested against) or encoded from simulated round broadcasts, cut into arbitrary chunks
with boot text and corruption in between. Assertions are on what the source yields, its counters, and the dashboard
payload the stream produces. No port is opened.
"""
import json
import random
from pathlib import Path

import pytest

from csi.gateway_records import (GatewaySource, Heard, NodeStatus, RecordError, decode_record,
                                 encode_heard, encode_status, encode_text)
from csi.room import Walk, load_room
from csi.dashboard import DashboardState
from csi.simulate import simulate_rounds

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "gateway_record_v1.json").read_text())
RECORDS = {case["name"]: case for case in GOLDEN["records"]}
BAD = {case["name"]: case for case in GOLDEN["bad"]}
BROADCAST = bytes.fromhex(RECORDS["heard_round_broadcast"]["record"]["payload"])
BOOT_TEXT = b"ESP-ROM:esp32c5-eco2-20250121\r\nrst:0x1 (POWERON),boot:0x18 (SPI_FAST_FLASH_BOOT)\r\n"
ROLE_CODES = {"transceiver": 0, "gateway": 1}   # the role byte of docs/gateway-record.md
TR8 = load_room(FIXTURES / "room_tr8.json")


def as_fields(record) -> dict:
    if isinstance(record, Heard):
        return {"type": "heard", "rx_time_us": record.rx_time_us, "rssi": record.rssi, "payload": record.payload.hex()}
    if isinstance(record, NodeStatus):
        return {"type": "status", "serial": record.serial, "uptime_us": record.uptime_us,
                "reset_reason": record.reset_reason, "role": ROLE_CODES[record.role], "channel": record.channel,
                "counters": list(record.counters)}
    return {"type": "text", "text": record.text}


def encode(fields: dict) -> bytes:
    if fields["type"] == "heard":
        return encode_heard(fields["rx_time_us"], fields["rssi"], bytes.fromhex(fields["payload"]))
    if fields["type"] == "status":
        return encode_status(fields["serial"], fields["uptime_us"], fields["reset_reason"], fields["role"],
                             fields["channel"], fields["counters"])
    return encode_text(fields["text"])


def chunked(data: bytes, seed: int = 0, t0: float = 100.0) -> list[tuple[float, bytes]]:
    """``data`` cut at random places into chunks 1 to 60 bytes long, 1 ms apart."""
    rng, out, i = random.Random(seed), [], 0
    while i < len(data):
        n = rng.randint(1, 60)
        out.append((t0 + 0.001 * len(out), data[i:i + n]))
        i += n
    return out


@pytest.mark.parametrize("name", sorted(RECORDS))
def test_golden_records_decode_to_their_fields_and_encode_to_their_bytes(name):
    case = RECORDS[name]
    record, length = decode_record(bytes.fromhex(case["hex"]))
    assert as_fields(record) == case["record"]
    assert length == len(bytes.fromhex(case["hex"]))
    assert encode(case["record"]).hex() == case["hex"]


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_records_are_rejected_for_their_reason(name):
    with pytest.raises(RecordError) as e:
        decode_record(bytes.fromhex(BAD[name]["hex"]))
    assert e.value.reason == BAD[name]["reason"]


def test_heard_records_come_out_as_round_broadcasts_whatever_the_chunking():
    data = BOOT_TEXT + bytes.fromhex(RECORDS["heard_round_broadcast"]["hex"]) * 3
    for seed in range(20):
        source = GatewaySource(chunked(data, seed))
        out = [payload for _, payload in source if payload is not None]
        assert out == [BROADCAST] * 3
        assert source.counts()["records"] == {"heard": 3}
        assert source.counts()["skipped_bytes"] == len(BOOT_TEXT)


def test_a_corrupt_record_costs_only_itself_and_is_counted():
    good = bytes.fromhex(RECORDS["heard_round_broadcast"]["hex"])
    corrupt = bytes.fromhex(BAD["crc_mismatch"]["hex"])
    source = GatewaySource([(1.0, good + corrupt + good + b"\x00junk" + good)])
    assert [payload for _, payload in source] == [BROADCAST] * 3
    counts = source.counts()
    assert counts["rejected"] == {"crc": 1}
    assert counts["records"] == {"heard": 3}


def test_status_and_text_records_are_kept_on_the_source_not_passed_on():
    status = bytes.fromhex(RECORDS["gateway_status"]["hex"])
    text = bytes.fromhex(RECORDS["text_reply"]["hex"])
    heard = bytes.fromhex(RECORDS["heard_round_broadcast"]["hex"])
    source = GatewaySource([(1.0, status + text + heard)])
    assert [payload for _, payload in source] == [BROADCAST]
    assert source.status.serial == "10:BD:A3:CC:85:BC" and source.status.counters[0] == 100
    assert source.status.role == "gateway"
    assert list(source.texts) == ["ok role=gateway channel=6"]
    assert source.counts()["records"] == {"heard": 1, "status": 1, "text": 1}


def test_a_quiet_port_yields_heartbeats():
    heard = bytes.fromhex(RECORDS["heard_round_broadcast"]["hex"])
    source = GatewaySource([(1.0, None), (2.0, heard), (3.0, b""), (4.0, None)])
    assert list(source) == [(1.0, None), (2.0, BROADCAST), (3.0, None), (4.0, None)]


def test_records_arriving_together_are_spread_by_the_gateways_receive_times():
    # A USB read often returns several records at once; their host times come from the gateway's receive times,
    # anchored at the newest record's arrival, so the order and spacing of the round broadcasts survive.
    data = b"".join(encode_heard(t_us, -50, BROADCAST) for t_us in (1_000_000, 1_025_000, 1_050_000))
    source = GatewaySource([(10.0, data)])
    times = [host_ts for host_ts, _ in source]
    assert times == pytest.approx([9.95, 9.975, 10.0])


def test_a_simulated_rig_through_the_gateway_reaches_the_same_payload_as_direct():
    # The input seam: the gateway source must hand the pipeline exactly what the simulator would.
    walk = Walk(((1.0, 1.5), (3.0, 1.5)), speed_mps=0.4, loop=True)
    direct = list(simulate_rounds(TR8, walk, duration_s=4.0, seed=3, t0=1000.0))
    data = b"".join(encode_heard(int((ts - 1000.0) * 1e6), -55, payload) for ts, payload in direct)
    via_gateway = [(ts, payload) for ts, payload in GatewaySource(chunked(data, seed=7, t0=2000.0))
                   if payload is not None]
    assert [payload for _, payload in via_gateway] == [payload for _, payload in direct]

    def payload_of(stream):
        state = DashboardState(TR8, mode="auto")
        for host_ts, payload in stream:
            state.push_broadcast(host_ts, payload)
        return state.snapshot()

    a, b = payload_of(direct), payload_of(via_gateway)
    assert a["status"]["broadcasts"] == b["status"]["broadcasts"]
    assert b["status"]["broadcasts"]["accepted"] == len(direct)
    assert {r["id"]: r["state"] for r in a["links"]} == {r["id"]: r["state"] for r in b["links"]}


def test_closing_the_source_closes_the_port():
    # MultiSource and the round recorder release a port by calling the stream's close().
    closed = []

    def chunks():
        try:
            while True:
                yield 1.0, None
        finally:
            closed.append(True)

    gen = chunks()
    source = GatewaySource(gen)
    it = iter(source)
    next(it)
    source.close()
    assert closed == [True]
