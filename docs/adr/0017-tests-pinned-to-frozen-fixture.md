# 0017. Tests are pinned to a frozen four-node room

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `f6f85fe` ("Pin tests to a frozen four-node room layout")
- **Related:** [0012](0012-host-ts-line-stream-seam.md), [0013](0013-packet-time-not-wall-clock.md), [0014](0014-room-model-as-config.md)

## Context

Many tests build on "the default room". The default room was the live rig config, and the rig is configuration that changes several times a day ([0014](0014-room-model-as-config.md)). Enabling `rx3` and moving it in `config/room.json` broke 17 tests that had nothing to do with `rx3`.

## Decision

- `tests/conftest.py` sets `CSI_ROOM` to `tests/fixtures/room_rig4.json` before any test imports `csi`, so `csi.room.DEFAULT_ROOM_PATH`, and the scripts' `--room` default when tests run them as subprocesses, point at a frozen four-node layout.
- `tests/test_guards.py` fails if the pin is lost, or if any test file names `config/room.json`.
- One module deliberately loads the real config: `tests/test_config_driven_nodes.py` checks that it loads and has at least two receivers and two senders.

## Alternatives considered

- **Tests read the live config** (the state before `f6f85fe`). Rejected: an edit to the rig broke 17 tests.
- **Update the tests with every rig change.** Not chosen: the rig changed eight times that day, and each change would have been a test change too.

## Consequences

- Reconfiguring the rig never changes a test outcome; the real config still gets a load test.
- The fixture is a snapshot of the rig at 15:25 on 2026-11-07. It lists `rx1` and `tx0` as ESP32-S3 and still carries `port_hint`; it is a layout for tests, not a parts list.
- Behaviour that depends on the exact live rig (for example the current link weights) is tested against the fixture's copy, not the rig's.

## Evidence

- **The failure that prompted it:** "Enabling rx3 and moving it in config/room.json broke 17 tests because they read the live rig config" (`f6f85fe`).
- **The guard:** `tests/test_guards.py`, `test_default_room_path_is_not_the_live_rig_config` and `test_no_test_file_refers_to_the_live_rig_config`.
