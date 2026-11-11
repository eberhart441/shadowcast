# Round broadcast format

**Version 2.** This page is the one definition of the bytes a transceiver sends in its slot each round. The
host's copy is `csi/broadcast.py` (layout) and `csi/round_auth.py` (authentication); the golden byte fixtures in
[`tests/fixtures/round_broadcast_v2.json`](../tests/fixtures/round_broadcast_v2.json) pin the host decoder,
encoder and verifier, and the firmware (`firmware/core/round_broadcast.h`, `firmware/core/round_auth.h`) must
reproduce the same fixtures. Change this page, the fixtures, the host modules and the firmware together, and bump
the version byte whenever the layout or the meaning of a field changes.

Version 2 has the layout of version 1; it makes the authentication tag real (see
[Authentication](#authentication), [ADR-0028](adr/0028-hmac-sha256-tag-and-round-number-freshness.md)). The v1
fixtures, [`tests/fixtures/round_broadcast_v1.json`](../tests/fixtures/round_broadcast_v1.json), are kept frozen:
each v2 valid case is its v1 case with version 2 and a real tag, and a v1 broadcast (a node on old firmware) is
rejected as `version`. Terms are as in [`GLOSSARY.md`](../GLOSSARY.md) (transceiver, round, slot, round
broadcast, link report).

A round broadcast is the payload of one ESP-NOW broadcast: a 26-byte **round header**, then one 4-byte **link
report** per other member of the round. Every multi-byte field is little-endian. With N members the payload
is 26 + 4 × (N − 1) bytes: 54 B at 8 nodes, 142 B at 30, inside ESP-NOW v1's 250 B.

## Round header (26 bytes)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | version | Format version, `2`. A receiver rejects any other value (reason `version`). |
| 1 | 1 | frame type | `1` = round broadcast with link reports. Other values are reserved for later tickets (join, raw mode) and rejected in v2 (reason `frame_type`). |
| 2 | 2 | round | Round number, u16, wraps at 65535. |
| 4 | 1 | slot | The slot this broadcast was sent in. |
| 5 | 1 | slots per round | Slots in this round, including the join slot. |
| 6 | 1 | schedule version | u8. Node indices only mean something within one schedule version. |
| 7 | 1 | node index | The sender's index, 0 to 31. Its bit must be set in the membership (reason `membership`). |
| 8 | 4 | membership | u32 bitmap: bit *i* is set when node index *i* is a member of the round. |
| 12 | 6 | node id | The sender's board serial (its base MAC), in transmission order: `10:BD:A3:CC:85:BC` is `10 bd a3 cc 85 bc`. |
| 18 | 8 | authentication tag | The first 8 bytes of HMAC-SHA256 under the kit key ([Authentication](#authentication)). A receiver rejects a broadcast whose tag does not verify (reason `auth`). In v1 this field was zeros and unchecked. |

The node id is not in the spec's header list. It is carried so that a round broadcast names its sender by full
node identity on its own, wherever it came from (a gateway, a recording, the simulator), and so that the
authentication tag covers the identity too. The ESP-NOW source address is not authenticated.

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

The score byte is the **score code** of [ADR-0021](adr/0021-on-node-score-streaming-window-and-score-code.md)
(`csi/score_code.py` on the host, `firmware/core/motion_score.h` on the node): 10 codes per octave. Code `0` is no score yet; code *c* from 1 to 255 stands for
the score 2^((*c* − 1)/10 − 10), from 2⁻¹⁰ ≈ 0.001 (code 1) to 2¹⁵·⁴ ≈ 43,000 (code 255). A score *s* encodes as
floor(10 × (log₂ *s* + 10) + 0.5) + 1, clamped to 1 to 255 (*s* ≤ 0 gives 1), so a decoded score is within
about 3.5 % of the score. `csi/broadcast.py` (`quantise_score`, `dequantise_score`) calls `csi/score_code.py`.

## Authentication

ESP-NOW cannot encrypt broadcasts, so a round broadcast carries its own tag. It gives authenticity, not secrecy:
anyone can read a round broadcast, but only a holder of the kit key can make one that passes. A steady train of
round broadcasts is easy to detect whatever its content.

**Tag.** The tag is the first 8 bytes of HMAC-SHA256(kit key, *m*), where *m* is the whole round broadcast with
its tag bytes 18 to 25 set to zero. It covers every other byte: version, frame type, round number, slot,
membership, node id and every link report, reserved flag bits included. A node signs a broadcast after encoding
it; a receiver decodes the layout first (a malformed one is rejected by its decode reason), then checks the tag.
The node computes HMAC-SHA256 with mbedTLS on the ESP32-C5's SHA accelerator; the host uses Python's `hmac`.

**Kit key.** 16 random bytes shared by every node of a kit and the dashboard host. `python scripts/node.py
keygen` writes it to `data/kit.key` (not in git; it never overwrites a key file); `python scripts/node.py PORT key
data/kit.key` stores it in a node's flash (NVS) and the node restarts. `dashboard.py --node-gateway` reads it from
`data/kit.key` (or `--kit-key PATH`) and refuses to start without it. Keys are compared by their **check value**,
the first 4 bytes of HMAC-SHA256(key, `shadowcast kit key check`) as 8 hex digits: a node answers `node.py PORT
key` with its check, and the dashboard shows the host's in `status.messages`. Neither ever shows the key. A
transceiver without a key only listens; a gateway needs none (it forwards bytes undecoded).

The **test kit key** `000102030405060708090a0b0c0d0e0f` is published: the fixtures and the simulator use it, the
host defaults to it for `--simulate` and `--replay-rounds` (pass `--kit-key` to replay a real kit's recording),
and neither `node.py` nor a node accepts it as a kit key.

**Freshness (replay).** A copied round broadcast keeps a valid tag, so the round number guards against replay.
Every receiver (the host, per board serial; a node, per node id) keeps the last fresh round number of each sender
and when it was heard. A broadcast whose tag verifies is **fresh** if its round number is 1 to 16 + *t* ahead of
that last fresh one (mod 2^16), where *t* is the milliseconds since it; anything else, the same round again, an
older one, or one too far ahead, is a **replay** (reason `replay`) and is not used. The exception is a sender
silent for more than 2 s, with no fresh broadcast: its next one is taken at any round number, because a node that
restarted or renumbered its rounds starts again from wherever it is. The first broadcast heard from a sender is
fresh. On the host, de-duplication runs first, so a second gateway's copy is a `duplicate`, not a replay.

What it does not stop: while a node is silent for more than 2 s, a replay of its own old broadcasts can stand in
for it until it is heard again; and a copy recorded a whole number of round-number wraps ago (55 min at 20 Hz,
27 min at 40 Hz) can pass if it lands just ahead of the live round. A longer counter would close both and needs a
new format version. A node whose round number steps back (a renumbered schedule) loses its next 2 s of
broadcasts at every receiver.

## How the host reads it

- A link is (receiving node, transmitting node), labelled `<receiver><-<transmitter>` with their names in
  `config/room.json`, found by board serial. The receiving node is the round broadcast's node id; the
  transmitting node is the node whose own round broadcasts, in the same schedule version, carry that node
  index. Until the host has heard a node's own round broadcast, reports about it cannot be placed and are
  counted as unplaced.
- `heard` 0 makes the link NO_SIGNAL. A warming flag or score 0 makes it WARMUP. Otherwise the score drives
  the link's detector as a packet's score does in the line pipeline.
- Bytes that fail to decode are counted by reason (`short`, `version`, `frame_type`, `length`, `membership`)
  and dropped. Then the tag is checked: one that does not verify is counted as `auth` before anything else
  looks at it, so a forgery cannot take the place of the real broadcast. A round broadcast whose node id is not in
  the room config is counted as an unknown node; one that is stale for its sender as `replay`.
- Several gateways may hear one round broadcast. The host places the first copy of (node id, schedule version,
  round number); any other copy within 2 s of host time, from any gateway, is counted as a `duplicate` and not
  placed again. Two gateways therefore never double-count a link report. A node must not send two different
  round broadcasts with the same round number in one schedule version.
- A live round broadcast that arrives while the host's queue is full (the host fell behind) is dropped and
  counted as `queue_overflow`; a recorded one waits. Every reason, unknown nodes and per-gateway counts are in
  the dashboard payload at `status.broadcasts` (`csi/rounds.py`), and unknown nodes, rejections and drops also
  raise a line in `status.messages`.
