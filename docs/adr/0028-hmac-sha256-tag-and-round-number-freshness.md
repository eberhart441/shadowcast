# 0028. Round broadcasts carry a truncated HMAC-SHA256 tag under a kit key, and the round number guards replay

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #16 (eberhart441/shadowcast), spec #2 (user story 16, "Ingest verifies version and authentication tag")
- **Related:** [0022](0022-host-time-round-broadcast-seam.md), [0026](0026-one-node-image-runtime-settings-gateway-records.md)

## Context

Round broadcasts are unacknowledged ESP-NOW broadcasts, and ESP-NOW cannot encrypt broadcasts (report 07 §5.6).
Without a check, any transmitter on the channel can inject link reports that read as an empty room or as motion,
or upset a node's slot timing. Format v1 reserved an 8-byte tag at bytes 18 to 25, written as zeros and never
checked. The tag has to be cheap on the node (a node at 30 nodes and 20 Hz checks about 580 broadcasts a second
and signs 20) and must not add a dependency to the host.

## Decision

- **Format v2:** the v1 layout with version byte 2. The tag is the first 8 bytes of HMAC-SHA256(kit key, the
  broadcast with its tag bytes set to zero), covering every other byte. A v1 broadcast is rejected as `version`, so
  a node on old firmware reads differently from one with the wrong key (`auth`).
- **Kit key:** 16 random bytes per kit, made by `scripts/node.py keygen` into `data/kit.key`, stored on each node
  in NVS by `scripts/node.py PORT key FILE`, read by the dashboard (`--kit-key`, default `data/kit.key`). Keys are
  compared by an 8-hex-digit check value; the key itself is never printed. A transceiver without a key only
  listens. A published test key serves the fixtures and the simulator and is refused as a kit key.
- **Freshness:** per sender, a broadcast is fresh when its round number is 1 to 16 + 1 per millisecond ahead of the
  last fresh one; any other round is a replay, except after 2 s of silence from that sender, when any round is
  taken (a restart or a renumbering). The host checks the tag before de-duplication and freshness after it.
- **Node:** mbedTLS's PSA API on the C5's SHA accelerator. The portable core (`firmware/core/round_auth.c`) owns
  the coverage and the freshness rule and takes the HMAC as a function, so the PC tests run it with a reference
  HMAC-SHA256 against the same fixtures as the host. A forged or replayed broadcast is counted as rejected and
  reaches neither the scheduler nor the link table.

## Alternatives considered

- **AES-128-CMAC**, which report 07 also names: as strong for this use and also accelerated on the C5, but the host
  would need a third-party AES package (the stdlib has none). HMAC-SHA256 is in Python's stdlib and mbedTLS. Ruled
  out on the host dependency.
- **Keep version 1 and start checking the tag:** would change the meaning of a field without a version bump, and a
  node on old firmware would read as a forger. Ruled out.
- **A longer replay counter** (an epoch or a 32-bit round number in the header): closes the gaps below, but changes
  the layout every later ticket builds on. Deferred until a format change is needed anyway.
- **Freshness per (node, schedule version):** would let a schedule restart its round numbers, but then a recording of
  any schedule version not heard in the last 2 s passes at any time. Ruled out: per node only.
- **ESP-NOW's own encryption:** unicast only, at most 17 peers. Not possible for round broadcasts.

## Consequences

- Authenticity, not secrecy: round broadcasts stay readable, and the beacon train stays detectable.
- Replay protection is bounded by the u16 round number and the 2 s resync: a silent node can be impersonated by
  replays of its own old broadcasts until it is heard again, and a copy recorded a whole number of wraps ago
  (55 min at 20 Hz) can pass if it lands just ahead of the live round.
- The round scheduler (#14) must keep each node's round numbers counting forward across schedule changes: a node
  whose round number steps back loses 2 s of broadcasts at every receiver.
- Every recording of a real kit needs that kit's key to replay (`--replay-rounds --kit-key`); the recording does not
  hold the key.
- Losing the key file means re-keying every node; one leaked key means re-keying the kit.

## Evidence

The tags in `tests/fixtures/round_broadcast_v2.json` were computed with openssl. The cost on the node was not
measured on hardware yet: the node measures it at start-up (`scripts/node.py PORT key` shows `sign_us` and
`verify_us` for a 30-node broadcast). The estimate, six SHA-256 blocks per 142-byte broadcast at a few microseconds
each on the accelerator plus the PSA call overhead, is 20 to 40 µs per broadcast, about 2 % of one core at 30 nodes
and 20 Hz; in software it would be about twice that.
