/*
 * Over-the-air updates on the node: ESP-IDF glue over firmware/core/ota_core.h (docs/ota.md, docs/adr/0029).
 *
 * The rest of the node image talks to this module through these five calls only:
 *
 *   node_ota_boot      at boot, after node_auth_start and before the radio starts. In maintenance mode (an update
 *                      or report job is stored) it joins the maintenance network, does the job and restarts: it
 *                      never returns. Otherwise it returns, and a new image starts its health check.
 *   node_ota_heard     every ESP-NOW body a transceiver hears, before rb_decode: true if it was a maintenance
 *                      command (consumed; a verified command for this node stores the job and restarts).
 *   node_ota_tick      about every 50 ms: the health check of a new image (commit, or roll back and restart).
 *   node_ota_command   "ota" lines over USB: "ota" (status) on any node, "ota send <hex>" on a gateway, which puts a
 *                      host-signed maintenance command on the sensing channel.
 *
 * The update path never uses the sensing channel except to hear the command: the image comes over the maintenance
 * network (a WPA2 access point on the dashboard host, SSID and passphrase derived from the kit key), into the other
 * app slot, verified by its SHA-256 against the command and by ESP-IDF against the running image's signing key.
 */
#ifndef NODE_OTA_H
#define NODE_OTA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "node_settings.h"

void node_ota_boot(const uint8_t serial[6], uint8_t sensing_channel, bool gateway);

bool node_ota_heard(const uint8_t *body, size_t len, int64_t t_us);

/* `member`: the round scheduler has this node as a member; `heard`, `sent`: the status counters since boot. */
void node_ota_tick(bool member, uint32_t heard, uint32_t sent);

bool node_ota_command(const char *line, char *reply, size_t n, node_cmd_t *action);

#endif /* NODE_OTA_H */
