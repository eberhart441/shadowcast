# 0030. Over-the-air updates: a signed maintenance command on the round, the image pulled from a maintenance network off the sensing channel, A/B slots with a health check

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #18 (eberhart441/shadowcast), spec #2 (user story 44, "Firmware management")
- **Related:** [0026](0026-one-node-image-runtime-settings-gateway-records.md), [0027](0027-round-scheduler.md), [0028](0028-hmac-sha256-tag-and-round-number-freshness.md)

## Context

Thirty nodes cannot each need a USB cable for every firmware change. Spec #2 asks for a signed image, A/B
partitions with rollback, a staged rollout, and a transport that does not load the sensing channel while the rig is
sensing. A node image is about 0.9 MB (ticket #13: 0xdbae0 B, 14 % of a 1 MB factory partition free), and the
board has one 2.4/5 GHz radio, which sits on the sensing channel all the time. Every round broadcast is
unacknowledged (ADR-0026); nothing on the sensing channel can carry a megabyte reliably without ACKs or repair.
The kit key (ADR-0028) is symmetric and sits in every node's flash, so anyone who takes one node home has it.

## Decision

- **Trigger on the round, transfer off it.** The host tells chosen nodes to update with one **maintenance
  command** (frame type 8 in the round broadcast format, `docs/round-broadcast.md`): the 26-byte header, signed with
  the kit key like every frame, then the command (update or report), the image's size and SHA-256, the server port,
  the maintenance network's channel, a host time stamp `issued`, and up to 13 target serials. The gateway sends the
  host-signed bytes on the sensing channel (`ota send <hex>` over USB) three times, 0.2 s apart: about 0.5 ms of
  airtime per command, never during normal sensing. Nothing else about an update touches the sensing channel.
- **Maintenance mode.** A target stores the job in NVS and restarts into maintenance mode: no ESP-NOW, no CSI. It
  joins the **maintenance network**, a WPA2 access point the dashboard host provides (a laptop hotspot or hostapd on
  the Pi), on a channel that does not overlap the sensing channel, 5 GHz preferred. Its SSID is
  `shadowcast-<kit key check>` and its passphrase is derived from the kit key
  (`HMAC-SHA256(kit key, "shadowcast maintenance network")`, first 12 bytes as hex), so no node needs a second
  provisioning step. With the channel given in the command the node probes only that channel; with channel 0 it
  scans passively. A network found on a channel that overlaps the sensing one is refused.
- **Pull over plain HTTP from the host.** `GET http://<DHCP gateway>:<port>/node.bin?serial=<serial>`, written
  straight into the other app slot. The node refuses the image unless its size and SHA-256 match the signed command
  (so a rogue server cannot even serve an older signed image), and ESP-IDF refuses it unless its signature verifies
  (below). The node then reports what happened (`POST /report`, a text line with an HMAC tag under the kit key),
  and restarts.
- **Signed images: ESP-IDF's signed apps, without hardware secure boot.** `CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT`
  with the Secure Boot V2 RSA-3072 scheme and `SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`: `esp_ota_end` verifies the
  new image's signature block against the public key in the running image's own signature block. The **signing
  key** is a separate RSA-3072 key pair (`espsecure generate-signing-key`); the private half stays on the user's
  machine (`data/ota-signing.pem`, not in git, not in CI), and CI builds an unsigned image that the user signs
  (`espsecure sign-data`). The first image on each board must be a signed one flashed over USB; a node running an
  unsigned image refuses every update. The kit key is never the signing key. The host checks an image the same way
  before serving it (`scripts/ota.py check`: format, appended SHA-256, signature block, RSA-PSS signature, signer)
  and refuses an unsigned, corrupt or foreign-signed one.
- **A/B slots with rollback.** A new partition table (`firmware/node/partitions.csv`, 4 MB flash): NVS where it was
  (settings and kit key survive the USB reflash), otadata, phy, and two 1.875 MB app slots `ota_0`/`ota_1`.
  `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`: a new image boots pending verification. Its **health check** passes when,
  within 120 s of boot, a transceiver is a member of a round and has heard 20 round broadcasts from other members
  and sent 20 of its own (a gateway: heard 20 frames). Then the image is marked valid. If the deadline passes, the
  node marks it invalid and restarts into the old image; if the new image crashes, hangs (task watchdog) or browns
  out first, the bootloader rolls back on the next boot by itself. The old image then reports the rollback.
- **Staged rollout from the host.** `scripts/ota.py rollout` checks the image, serves it, and updates the room's
  nodes in stages of 2, then 6, then the rest (13 per command at most). A stage passes only when every target has
  reported `committed` and been heard on the round again; anything else stops the rollout with the nodes left on
  whatever they run (the old image, or the new one where it passed).

## Alternatives considered

- **The image over ESP-NOW on the sensing channel, in a maintenance window:** pauses the whole rig for the
  transfer (about 4,000 ESP-NOW v1 frames per node, plus a repair protocol, since broadcasts have no ACK), and
  loads the sensing channel by definition. Report 08 calls ESP-NOW "not a practical OTA path". Ruled out.
- **ESP-NOW unicast on a second channel:** ACKed, but still a custom transfer protocol with resume and repair to
  write and test, and ~1 Mbit/s at best. Ruled out against standard Wi-Fi and HTTP, which ESP-IDF already has.
- **HTTPS (esp_https_ota as report 08 suggests):** adds secrecy, which the image does not need (the repo is
  public), and costs a certificate on the host, its pin or CA on every node, and TLS code in the image. Integrity
  comes from the signature and the SHA-256 in the signed command. Not chosen; the download uses esp_http_client and
  esp_ota_ops directly so the node can check the SHA-256 of exactly the bytes it wrote.
- **The node runs a server and the host pushes:** a listening socket on every node. Ruled out.
- **Hardware secure boot v2:** burns the key digest into eFuses for good; a lost signing key or a mistake bricks
  the boards' update path. The image signature format is the same, so it can be switched on for production boards
  later without changing the update path. Deferred.
- **An HMAC under the kit key as the image signature:** every node holds the kit key, so one stolen node could sign
  firmware for the whole kit. Ruled out (also by the ticket): the signing key is asymmetric and never on a node.
- **Our own Ed25519 or ECDSA signature:** own crypto code on the node, and no stdlib support on the host. Ruled out
  against the scheme ESP-IDF already verifies; the host side only verifies RSA-PSS, a few lines of `pow`.
- **Maintenance network credentials provisioned per node over USB:** one more step per board and per kit. Derived
  from the kit key instead; the passphrase is no more secret than the kit key, which already lets its holder
  command a node into maintenance mode (a node off the round, not new firmware).
- **An OTA button on the dashboard (spec "Commands"):** the rollout is a library (`csi/ota.py`) with the gateway
  write as a parameter, so the dashboard can drive it later; for now `scripts/ota.py` runs with the dashboard
  stopped, because both need the gateway port.

## Consequences

- Every board needs one more USB flash: the new partition table, a bootloader with rollback, and a signed image.
  After that, updates are wireless.
- Updating a node takes it off the round for the download plus two restarts (about 30 to 60 s, then 11 s of
  warm-up per link). A staged rollout keeps the rest of the rig sensing; a node that updates shows as missing on
  the dashboard meanwhile.
- The maintenance network must be up, on a channel clear of the sensing channel, while a rollout runs. A node that
  cannot reach it within 20 s restarts into the image it had; nothing changes.
- The health check needs a running round: a node updated alone, with no other member on the air, rolls back.
- A replayed maintenance command is refused by its `issued` time (each node keeps the newest it executed), so an
  eavesdropper without the kit key can at most replay a command the node has already run; with the kit key one can
  take nodes off the round, not install firmware.
- Losing `data/ota-signing.pem` means every board needs a USB flash of an image signed with a new key.
- Two images per board now fit with about 45 % of each slot free; the 4 MB setting works on 4 MB and 8 MB flash.

## Evidence

Not measured on hardware: no board has run this yet (rig check `docs/rig-checks/ota.md`). The image size with the
update code is from the local ESP-IDF 6.0 build (`firmware/README.md` "Over-the-air updates"). The host's image
check is pinned to an image signed by `espsecure` 5.6.0 (`tests/fixtures/ota_signed_image.bin`).
