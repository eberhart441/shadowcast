# Round broadcast format

**Version 1.** This page is the one definition of the bytes a transceiver sends in its slot each round. The
host's copy is `csi/broadcast.py`; the golden byte fixtures in
[`tests/fixtures/round_broadcast_v1.json`](../tests/fixtures/round_broadcast_v1.json) pin the host decoder and
encoder, and the firmware's encoder and decoder must reproduce the same fixtures. Change this page, the
fixtures, `csi/broadcast.py` and the firmware together, and bump the version byte whenever the layout or the
meaning of a field changes. Terms are as in [`GLOSSARY.md`](../GLOSSARY.md) (transceiver, round, slot, round
broadcast, link report).

A round broadcast is the payload of one ESP-NOW broadcast: a 26-byte **round header**, then one 4-byte **link
report** per other member of the round. Every multi-byte field is little-endian. With N members the payload
is 26 + 4 × (N − 1) bytes: 54 B at 8 nodes, 142 B at 30, inside ESP-NOW v1's 250 B.

## Round header (26 bytes)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | version | Format version, `1`. A receiver rejects any other value (reason `version`). |
| 1 | 1 | frame type | `1` = round broadcast with link reports. Other values are reserved for later tickets (join, raw mode) and rejected in v1 (reason `frame_type`). |
| 2 | 2 | round | Round number, u16, wraps at 65535. |
| 4 | 1 | slot | The slot this broadcast was sent in. |
| 5 | 1 | slots per round | Slots in this round, including the join slot. |
| 6 | 1 | schedule version | u8. Node indices only mean something within one schedule version. |
| 7 | 1 | node index | The sender's index, 0 to 31. Its bit must be set in the membership (reason `membership`). |
| 8 | 4 | membership | u32 bitmap: bit *i* is set when node index *i* is a member of the round. |
| 12 | 6 | node id | The sender's board serial (its base MAC), in transmission order: `10:BD:A3:CC:85:BC` is `10 bd a3 cc 85 bc`. |
| 18 | 8 | authentication tag | Reserved for authenticated round broadcasts; v1 senders write zeros and v1 receivers do not check it. |

The node id is not in the spec's header list. It is carried so that a round broadcast names its sender by full
node identity on its own, wherever it came from (a gateway, a recording, the simulator), and so that the
authentication tag, once defined, covers the identity too. The ESP-NOW source address is not authenticated.

## Link reports (4 bytes each)

There is one link report for every member of the round other than the sender, in ascending node index. The
k-th report is about the link from the k-th such member (the transmitter) to the sender of the round broadcast
(the receiving node). A member the sender never heard still gets a report, with `heard` 0. The payload length
must be exactly 26 + 4 × (members − 1) (reason `length`).

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | score | Quantised score, u8. `0` means no score yet. |
| 1 | 1 | rssi | i8, dBm of the last round broadcast heard from the transmitter. `-128` means none. |
| 2 | 1 | heard / since | High nibble: round broadcasts heard from the transmitter over the last 15 rounds (0 to 15). Low nibble: rounds since it was last heard (0 = this round; 15 = 15 or more, or never). |
| 3 | 1 | flags | Bit 0: baseline warming. Bit 1: possible move. Bit 2: raw mode active. Bits 3 to 7 are reserved: sent as 0, ignored on decode. |

The link reports describe the previous round: each node sends, in its slot, what it measured on the round
broadcasts it heard since its own last slot.

The spec says "heard in the last 16 rounds" in a 4-bit field. Version 1 counts over the last **15** rounds, so
that 15 means "heard every round" exactly instead of saturating.

### Score quantisation

**Provisional.** The C scorer ticket owns the u8 scale and will replace `quantise_score` and `dequantise_score`
in `csi/broadcast.py`, and this paragraph, with its documented scale. Until then: `0` is no score; 1 to 255 are
log-spaced from 10⁻⁴ (1) to 10⁴ (255), a step of about 7.5 %, so a dequantised score is within about 4 % of the
score. Scores below the range read as 1, above it as 255. Nothing else on the host depends on the scale.

## How the host reads it

- A link is (receiving node, transmitting node), labelled `<receiver><-<transmitter>` with their names in
  `config/room.json`, found by board serial. The receiving node is the round broadcast's node id; the
  transmitting node is the node whose own round broadcasts, in the same schedule version, carry that node
  index. Until the host has heard a node's own round broadcast, reports about it cannot be placed and are
  counted as unplaced.
- `heard` 0 makes the link NO_SIGNAL. A warming flag or score 0 makes it WARMUP. Otherwise the score drives
  the link's detector as a packet's score does in the line pipeline.
- Bytes that fail to decode are counted by reason (`short`, `version`, `frame_type`, `length`, `membership`)
  and dropped; a round broadcast whose node id is not in the room config is counted as an unknown node.
