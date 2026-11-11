# Rig check: over-the-air updates (ticket #18)

Checks on hardware what the tests check on the PC and what only a board can show: two boards update over the air
and rejoin the round; a node itself refuses an unsigned, a corrupt and a foreign-signed image (not only the host);
a new image that fails its health check, or loses power during it, rolls back. The procedure is
[`docs/ota.md`](../ota.md); the decision is [ADR-0030](../adr/0030-ota-maintenance-network-signed-images.md).

**Result:** not yet run. Record the result at the end of this page and tick the ticket's rig-check box.

## 0. What you need

- The rig of [`two-nodes.md`](two-nodes.md) (transceivers A and B, a gateway G, each with its own supply), after
  that check and [`authentication.md`](authentication.md) pass, with the kit key on A and B.
- The dashboard host with Wi-Fi that can run an access point (Windows Mobile Hotspot, or hostapd on the Pi),
  ideally on 5 GHz.
- ESP-IDF 6.0 (`espsecure`, `esptool`). Identify every board by its serial before flashing
  (`python scripts/ports.py`); confirm each has at least 4 MB of flash with `esptool -p <PORT> flash-id`.

## 1. Signing key and the first USB flash

1. Make the signing key and its digest ([`docs/ota.md`](../ota.md) "Once per kit").
2. Build the image, sign it, and `python scripts/ota.py check build/node-signed.bin` (expect `ok ...` with the
   version). Also `python scripts/ota.py check build/node.bin`: expect `refused (unsigned)`.
3. Flash A, B and G over USB with the signed image and the new partition table ([`docs/ota.md`](../ota.md) "Once
   per board"). G keeps `role=gateway` (settings survive in NVS). For each, `python scripts/node.py <PORT> show`
   still shows its settings, and `python scripts/node.py <PORT> ota` shows `slot=ota_0`, its version, `last_issued=0`.
4. Power A and B from their own supplies, start the dashboard as in `two-nodes.md` step 4, confirm both links are
   live, then stop the dashboard.

## 2. A second image to update to

Make a change the version string shows (any new commit, or build with `-DPROJECT_VER=ota-check-2`), build into
another directory, sign it as `node-v2-signed.bin`, and check it with `ota.py check`. Record both versions.

## 3. The node refuses bad images

Bring up the maintenance network (`python scripts/ota.py network` gives its SSID and passphrase) and note its
channel. Each run below targets A only; the host check is skipped on purpose with `--unchecked`, so the node's own
checks are what refuse. A must stay on the old image every time.

| Image served to A | Make it | Expected report from A |
|---|---|---|
| unsigned | `build-v2/node.bin` | `refused_image` |
| corrupt | a copy of `node-v2-signed.bin` with one byte in the middle changed (e.g. Python: flip byte 0x40000) | `refused_image` |
| signed by another key | sign with a second key from `espsecure generate-signing-key` | `refused_image` |

```
python scripts/ota.py rollout <IMAGE> --gateway <G PORT> --nodes <A> --ap-channel <channel> --unchecked
```

Expect, within about a minute, `<A serial>: refused_image ...` and `rollout stopped`. A is heard on the round
again within a minute (the dashboard shows it live). On A's USB, `ota` shows the old version and slot. Without
`--unchecked`, each of these is refused by `ota.py` before anything is sent.

## 4. Two boards update and rejoin the round (the ticket's check)

```
python scripts/ota.py rollout build-v2/node-v2-signed.bin --gateway <G PORT> --ap-channel <channel>
```

With `config/room_tr2.json` as the room (`--room`), both A and B are the first stage. Expect for each: a
`fetching the image` line, then about 2 minutes later `<serial>: committed (slot ota_1, version <v2>)`, then
`rollout done` once both are heard on the round again. Record the time from the command to each report and to
`rollout done`. Start the dashboard: both links live, scores warming up then normal. On each board's USB, `ota`
shows `slot=ota_1 state=valid version=<v2>`.

## 5. A failed health check rolls back

1. Switch B off. Roll the first image (signed) back out to A alone:
   `ota.py rollout build/node-signed.bin --gateway <G PORT> --nodes <A> --ap-channel <channel>`.
   A boots it and finds no other member: after 120 s the health check fails and A rolls back.
2. Expect `<A serial>: rolled_back (slot ota_1, version <v2>)` and `rollout stopped`. On A's USB, `ota` shows
   `slot=ota_1 state=valid version=<v2>`.
3. Power cut: with B still off, start the same rollout again. About 30 s after A's `fetching the image` line (it
   is then in its health check, which cannot pass alone), unplug A's power for 5 s and plug it back. The bootloader
   rolls back by itself, before the health check could decide: expect `rolled_back` again within about a minute of
   the replug, and A back on `<v2>`. Switch B on again; both links come back.

## 6. The rest of the rig keeps sensing (if a third transceiver is at hand)

With three transceivers, update one (`--nodes`) while the dashboard runs on a second gateway, or watch the other
two's link between them on a recording made with `--record`: its packet rate must not drop while the third
downloads. This checks that the maintenance network does not load the sensing channel.

## Result

| Date | Boards | Step 3 (3 refusals) | Step 4 (times) | Step 5 (two rollbacks) | Step 6 | Notes |
|---|---|---|---|---|---|---|
| | | | | | | |
