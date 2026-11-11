# Rig check: authenticated round broadcasts (ticket #16)

Checks on hardware what the tests check on the PC: the node's mbedTLS tag matches the host's, a node with another
kit key is rejected and counted, and what signing and verifying cost on an ESP32-C5. Run it on the rig of
[`two-nodes.md`](two-nodes.md), after that check passes. The format is
[`docs/round-broadcast.md`](../round-broadcast.md) "Authentication".

**Result:** not yet run. Record the result at the end of this page and tick the ticket's rig-check box.

## 1. The cost per broadcast, and the self-test

With a transceiver on USB and the dashboard stopped, run `python scripts/node.py <PORT> key`. Expect

```
ok key check=<8 hex> sign_us=<n> verify_us=<n> forged=<n> replayed=<n>
```

`sign_us` and `verify_us` are the node's own measurement at start-up: the mean over 100 signs and 100 verifies of a
142-byte (30-node) round broadcast, HMAC-SHA256 through mbedTLS on the SHA accelerator. The estimate is 20 to 40 µs
each. Before the measurement the node checks mbedTLS against a golden fixture tag; if that fails, the start-up log
says `self-test FAILED` and the node only listens (`configured=no` in `show`). Record both numbers for both
transceivers.

## 2. Matching keys

Run the dashboard as in [`two-nodes.md`](two-nodes.md) step 4. `status.messages` names the host's kit key check;
it must equal the check both transceivers showed. After a minute, `GET /api/state` `status.broadcasts.rejected`:
`auth` and `replay` stay at 0 and `accepted` grows by about 80 per second (two nodes at 40 Hz).

## 3. A node with another key

1. Stop the dashboard. Make a second key, `python scripts/node.py keygen data/other.key`, and give it to
   transceiver B: `python scripts/node.py <PORT B> key data/other.key`. Power B from its charger again.
2. Start the dashboard. Expect `status.broadcasts.rejected.auth` to grow by about 40 per second (all of B's round
   broadcasts), a `status.messages` line naming `auth` and the kit key, and the links `n1<-n2` and `n2<-n1` to go
   NO_SIGNAL: A ignores B's broadcasts (they fail its check), so it reports B as not heard.
3. With A on USB (dashboard stopped), `python scripts/node.py <PORT A> key`: `forged` grows by about 40 per second,
   and `status` shows `rejected` growing at the same rate.
4. Give B the kit key again (`python scripts/node.py <PORT B> key data/kit.key`) and confirm step 2 again.

## Result

| Date | Board | sign_us | verify_us | Step 2 | Step 3 | Notes |
|---|---|---|---|---|---|---|
| | | | | | | |
