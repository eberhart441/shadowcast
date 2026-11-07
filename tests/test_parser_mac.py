"""The parser reads the sender MAC from the `mac` column (lowercased); lines without it get ''."""
import numpy as np

from csi.parser import DEFAULT_HEADER, WARMUP_PACKETS, CSIParser
from csi.parser import CSIPacket


def csi_text(mac: str, values=(3, 4, 6, 8)) -> str:
    cols = DEFAULT_HEADER.split(",")[:-1]
    v = {"type": "CSI_DATA", "mac": mac, "rssi": -40, "channel": 6, "local_timestamp": 1000,
         "len": len(values)}
    return ",".join(str(v.get(c, 0)) for c in cols) + f',"[{",".join(map(str, values))}]"'


def locked_parser(mac: str = "1a:00:00:00:00:00") -> CSIParser:
    ps = CSIParser()
    for _ in range(WARMUP_PACKETS):
        ps.parse_line(csi_text(mac), host_ts=0.0)
    return ps


def test_packet_carries_sender_mac_lowercased():
    pkt = locked_parser().parse_line(csi_text("1A:00:00:00:00:01"), host_ts=5.0)
    assert pkt.mac == "1a:00:00:00:00:01"


def test_mac_defaults_to_empty_string_when_header_has_no_mac_column():
    hdr = "type,local_timestamp,channel,rssi,len,data"
    ps = CSIParser()
    ps.parse_line(hdr)
    text = 'CSI_DATA,777,11,-55,4,"[3,4,6,8]"'
    for _ in range(WARMUP_PACKETS):
        ps.parse_line(text, host_ts=0.0)
    assert ps.parse_line(text, host_ts=1.0).mac == ""


def test_csi_packet_mac_defaults_to_empty_for_existing_callers():
    pkt = CSIPacket(host_ts=0.0, device_ts=1, rssi=-40, channel=6, amplitude=np.zeros(2, np.float32))
    assert pkt.mac == ""
