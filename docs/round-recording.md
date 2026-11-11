# Round recording format

**Version 1.** This page is the one definition of a round recording: the file `dashboard.py --record` writes
and `dashboard.py --replay-rounds` reads. The code is `csi/round_recording.py` (`RoundRecorder`, `load_rounds`);
`tests/test_round_recording.py` pins its behaviour. Change this page and that module together, and bump the
version whenever the layout or the meaning of a field changes. Why the file looks like this:
[ADR-0023](adr/0023-round-recordings.md). Terms are as in [`GLOSSARY.md`](../GLOSSARY.md).

A round recording keeps every round broadcast the run's sources delivered, exactly as delivered (undecoded,
rejected bytes included), with its host time and the name of its source, plus labels and the room config the
run used. Replaying it feeds the same bytes at the same host times through the input seam
([ADR-0022](adr/0022-host-time-round-broadcast-seam.md)), so the pipeline reaches the same link states and
room state as the live run. Raw mode CSI rides inside round broadcasts (frame type 5,
[`round-broadcast.md`](round-broadcast.md#raw-mode)), so a recording keeps it with no change to this format, and a
replay shows it again (`status.broadcasts.raw_csi`, the payload's `raw_mode`). `recording.raw_csi(kit_key)` lists it.

Every multi-byte field is little-endian. The suggested file suffix is `.rounds`.

## Preamble (14 bytes) and header

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 8 | magic | ASCII `SCROUNDS`. A file that does not start with it is not a round recording. |
| 8 | 2 | format version | u16, `1`. A reader refuses any version it was not built for, naming the version. |
| 10 | 4 | header length | u32: the bytes of header JSON that follow. |
| 14 | n | header | UTF-8 JSON object, below. |

The header object:

| Key | Meaning |
|---|---|
| `room` | The room config in `config/room.json` form (`room`, `nodes`, optional `link_weights`). A disabled node keeps only `"enabled": false`. Replay names senders by the serials in it. |
| `sources` | List of source names (for example a gateway's name). A record's source index points into it. At most 255. |
| `notes` | Free text. |
| `started` | Wall clock when recording began, seconds since the epoch. |

A reader ignores header keys it does not know, so a key can be added without a version bump.

## Records

Records follow the header to the end of the file. Each is a 14-byte record head, then its body:

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | kind | `1` = round broadcast, `2` = label. Any other kind is refused in version 1. |
| 1 | 8 | host time | f64, seconds: the host time the source stamped (a broadcast), or from which the label holds. |
| 9 | 1 | source | Index into the header's `sources` for a broadcast; `255` for a label. |
| 10 | 4 | body length | u32. |
| 14 | n | body | Kind 1: the round broadcast bytes ([`round-broadcast.md`](round-broadcast.md)). Kind 2: UTF-8 JSON object. |

- **Broadcast records** are in the order each source delivered them, so each source's own records are in its
  host-time order. Records of different sources interleave as they arrived; replay re-merges them by host time,
  as the live server does.
- **Label records** hold `{"label": L, ...}`, where L is one of `empty`, `walking`, `still_standing`,
  `still_sitting_or_lying` (the recording labels, ADR-0018). Other keys are extras, for example
  `{"label": "walking", "mark": "A3"}` for a floor mark. A label holds from its host time until the next label.
  Labels are stamped with the wall clock when set live, which is host time for a gateway and for the real-time
  simulator.
- Heartbeats are not recorded.
- **A cut-short last record** (the recorder was killed mid-write) is left out on reading and counted
  (`truncated_bytes`); the replay says so in `status.messages`. Anything else malformed is refused.

At 30 nodes and 20 rounds per second a recording grows by about 600 × (14 + 142) B, roughly 94 KB/s or
340 MB per hour. `load_rounds` reads the whole file into memory.

## Recording and replaying

```
python scripts/dashboard.py --simulate --room config/room_tr8.json --record data/run.rounds --label empty
    # while it runs: type "walking" (or "walking mark=A3") and Enter to switch label; Ctrl+C stops and closes
python scripts/dashboard.py --replay-rounds data/run.rounds --speed 1|N|max
```

In code:

```python
from csi.round_recording import RoundRecorder, load_rounds
from csi.replay import rounds_dashboard

with RoundRecorder("run.rounds", room, ["gw1"]) as rec:
    streams = rec.tee({"gw1": stream})        # or rec.write("gw1", host_ts, data)
    rec.label("walking", host_ts=t)           # host_ts defaults to the wall clock now
recording = load_rounds("run.rounds")         # RecordingFormatError if unusable
recording.broadcasts, recording.labels, recording.label_at(t), recording.room
recording.streams(speed=8)                    # {source: (host time, bytes)} paced; math.inf = no waiting
server = rounds_dashboard("run.rounds", speed=math.inf)   # a read-only DashboardServer in auto mode
for s in recording.raw_csi(kit_key):          # raw mode CSI, in host-time order, through the live ingest
    s.host_ts, s.source, s.link, s.csi.round, s.csi.rssi, s.csi.values   # "n1<-n5", int8 I/Q (imaginary, real)
```

To record raw CSI: start the dashboard with `--record`, click a link in the matrix and press "Stream raw CSI" (or
`POST /api/raw-mode {"link": "n1<-n5", "on": true}` on loopback), and turn it off the same way.
