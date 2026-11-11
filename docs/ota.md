# Over-the-air updates

How to update the node image on every board without a USB cable, and what each node does meanwhile. The decision
and its reasons are [ADR-0029](adr/0029-ota-maintenance-network-signed-images.md); the frame that starts an update
is the [maintenance command](round-broadcast.md#maintenance-command); the hardware check is
[`rig-checks/ota.md`](rig-checks/ota.md). Terms are as in [`GLOSSARY.md`](../GLOSSARY.md).

## In one paragraph

The host checks a signed image, serves it over HTTP on a **maintenance network** (a WPA2 access point on the
dashboard host, off the sensing channel), and sends a signed **maintenance command** through the gateway to 2 nodes.
Each of them restarts into **maintenance mode**, joins that network, downloads the image into its other app slot,
refuses it unless its size and SHA-256 match the command and ESP-IDF verifies its signature, and restarts into it.
The new image must pass a **health check** (a member of the round, 20 round broadcasts heard and 20 sent within
120 s); then it stays, otherwise the node rolls back to the old one. Either way the node reports the outcome to
the host and rejoins the round. When both nodes of a stage are back with the new image, the next stage (6 nodes,
then 13 at a time) follows. The rest of the rig keeps sensing throughout.

## Once per kit: the signing key

The signing key is an RSA-3072 key pair, separate from the kit key. Only its holder can make images the nodes take.
Make it with ESP-IDF's `espsecure` (in the ESP-IDF Python environment) and keep the private key off git and off CI:

```
espsecure generate-signing-key --version 2 --scheme rsa3072 data/ota-signing.pem
espsecure digest-sbv2-public-key --keyfile data/ota-signing.pem --output data/ota-signing.digest
```

`data/` is not in git. `data/ota-signing.digest` (the public key's SHA-256) is what `scripts/ota.py` trusts; it is
not secret. Losing `data/ota-signing.pem` means every board needs a USB flash of an image signed with a new key.

## Once per board: the USB flash

Boards that ran the single-app image need one more USB flash: the new partition table (two app slots,
`firmware/node/partitions.csv`, 4 MB flash; NVS stays where it was, so node settings and the kit key survive), a
bootloader with rollback, and a **signed** image. A board that runs an unsigned image refuses every update, because
it checks new images against the key in its own image's signature block.

```
cd firmware/node
idf.py -B build set-target esp32c5 && idf.py -B build build       # the image is built unsigned (as in CI)
espsecure sign-data --version 2 --keyfile ../../data/ota-signing.pem --output build/node-signed.bin build/node.bin
python ../../scripts/ota.py check build/node-signed.bin           # "ok ...": a node would take it
cd build && esptool --chip esp32c5 -p <PORT> -b 460800 write-flash --flash-mode dio --flash-freq 80m \
    --flash-size keep 0x2000 bootloader/bootloader.bin 0x8000 partition_table/partition-table.bin \
    0xf000 ota_data_initial.bin 0x20000 node-signed.bin
```

The board must have at least 4 MB of flash (`esptool -p <PORT> flash-id` shows it; check the board serial first).

## Every release

1. Build, sign and check the image as above (or sign CI's `node-image-esp32c5` artifact). `ota.py check` refuses an
   unsigned image (`unsigned`), a corrupt one (`digest` or `signature`), one signed by another key (`signer`) and one
   for another chip (`chip`).
2. Bring up the maintenance network on the dashboard host. `python scripts/ota.py network` prints its SSID
   (`shadowcast-<kit key check>`) and WPA2 passphrase, both derived from the kit key. Use 5 GHz if the host can; on
   2.4 GHz pick a channel at least 5 away from the sensing channel (sensing on 6: channel 1 or 11). A Windows Mobile
   Hotspot (band 5 GHz) or hostapd on the Pi both work; the host's address on it is where the nodes fetch from.
3. Stop the dashboard (the rollout needs the gateway's port), then:

   ```
   python scripts/ota.py rollout build/node-signed.bin --gateway <GATEWAY PORT> --ap-channel 36
   ```

   Nodes default to every transceiver with a serial in `config/room.json`, in its order; `--nodes n1,n2` picks some
   (names or serials). `--ap-channel 0` lets the nodes scan for the network passively (slower). The image server
   listens on port 8070 (`--port`). The rollout prints every report and stops at the first node that does not come
   back with the new image, naming it and its outcome.
4. Restart the dashboard. `python scripts/ota.py checkin --gateway <PORT>` asks every node to report its running
   image without changing anything.

## What a node does

| Step | Where | Ends with |
|---|---|---|
| hears a verified maintenance command naming it, newer than the last it ran | on the round | stores the job, restarts |
| maintenance mode: clears the job (so it never repeats), passive scan for the network, joins it | maintenance network | `no_network` or `overlap` if not found or on an overlapping channel |
| downloads `http://<access point>:<port>/node.bin` into the other slot, checks size and SHA-256, ESP-IDF verifies the image and its signature, selects it | maintenance network | `refused_size`, `refused_digest`, `refused_image`, `no_server`, `write_failed`: reports it, restarts into the old image |
| boots the new image, pending verification; joins the round as usual | on the round | health check |
| health check passes: marks the image valid | | restarts, reports `committed`, restarts, rejoins |
| health check fails after 120 s, or the image crashes, hangs or browns out first | bootloader | rolls back; the old image reports `rolled_back` and rejoins |

Maintenance mode never lasts more than 240 s. A report is one tagged text line POSTed to `/report`
(`ota-report 1 serial=.. issued=.. outcome=.. slot=.. image=.. version=.. tag=..`); the host refuses one without
the kit's tag. On the node's USB port, `ota` shows the running slot, its state, version and the last command run.

## Limits

- The health check needs a running round: a node updated alone, with no other member on the air, rolls back.
- A gateway is updated over USB (it is on the host); it never acts on a maintenance command.
- The maintenance command is broadcast on the sensing channel, about 0.5 ms of airtime per stage; the image never
  is. The maintenance network must be up and clear of the sensing channel for the whole rollout.
- Each updated node is off the round for its download and two restarts (about 30 to 60 s), then warms up its links
  again (about 11 s). The dashboard shows it missing meanwhile.
- `issued` is the host's clock in seconds. A host whose clock went backwards (a Pi without network time) sends
  commands the nodes ignore as old; set the clock first.
- No hardware secure boot: someone with a USB cable can still flash anything. The signature protects the wireless
  path only.
