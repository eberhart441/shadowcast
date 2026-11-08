# 0002. ESP32-C5 receivers only

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `5c705c9` (rx1 to C5, S3 rx3 disabled), `c3220bc`, `7cc6184`; `docs/agents/hardware.md` ("Receiver chip")
- **Related:** [0003](0003-one-shared-channel-at-40-hz.md), [0010](0010-two-noisy-room-calibration-methods.md)

## Context

A receiver's own noise shows up directly as false MOTION on every link it serves. Three chip families were on hand: the original ESP32, the ESP32-S3 and the ESP32-C5. During 2026-11-07 the rig changed chips several times (`git log -- config/room.json`), which produced side-by-side observations of each family as receiver and as sender.

## Decision

Every receiver is an ESP32-C5. Senders may be C5 or S3: both work. The original ESP32 is retired in both roles. The current rig is four C5 boards (`config/room.json`).

## Alternatives considered

- **ESP32-S3 receivers.** Tried and rejected: much noisier in a quiet room, and an S3 as a third receiver starved the other receiver (Evidence).
- **A mixed rig: C5 receivers plus an S3 for extra coverage.** Tried: an S3 `rx3` was enabled at 15:09 (`c3334f4`) and switched off at 15:51 (`5c705c9`, "The S3 receivers were much noisier than the C5 receiver").
- **The original ESP32.** Tried and retired: noisy as a receiver and as a sender.

## Consequences

- Receivers are built and flashed for `esp32c5`, which needs ESP-IDF 5.5 or newer (`firmware/README.md`). The firmware still builds for an S3 receiver, kept only for bringing one back, which the README advises against.
- The parser carries the C5 column layout and its three null subcarriers (`csi/parser.py`, `C5_HEADER`, `KNOWN_NULL_SUBCARRIERS`).
- Some evidence predates the all-C5 rig and has not been re-measured: the 0.3 heatmap weight on `rx1<-01` was set while `rx1` was an S3, and the only noisy-room calibration comparison ([0010](0010-two-noisy-room-calibration-methods.md)) was taken on `rx2` while it was an S3.
- A board swap at 21:47 briefly put an S3 back in as `rx2` (`33dbc43`); a C5 replaced it 20 minutes later (`7cc6184`). The decision held, but it shows how easily the rig drifts from it, which is one reason the rig is configuration ([0014](0014-room-model-as-config.md)).
- Test fixtures still list S3 nodes. They are frozen layouts, not a parts list ([0017](0017-tests-pinned-to-frozen-fixture.md)).

## Evidence

- **Quiet-room false MOTION:** links on an S3 receiver read MOTION 35% to 65% of the time, against 4% to 16% on the C5 receiver (README "Placement tips"; `docs/agents/hardware.md`). The captures behind these figures are not committed.
- **Third receiver:** an S3 cut the other receiver's packet rate from about 35 to 22 to 26 packets/s; a third C5 left every link at 34 to 38 packets/s (README "Scaling up", "Limits").
- **Original ESP32 as sender:** its two links ran at 6 to 17 packets/s, against about 38 for the S3-sender links (issue #9 "Further Notes"; README "Results and known limits").
