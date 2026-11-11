/*
 * The kit key and round broadcast authentication on the node: ESP-IDF glue over firmware/core/round_auth.h, with
 * HMAC-SHA256 from mbedTLS (PSA API) on the ESP32-C5's SHA accelerator. docs/round-broadcast.md "Authentication".
 *
 * The kit key lives in NVS (namespace "node", blob "kit_key") and is set over USB with one text command, handled by
 * node_auth_command (scripts/node.py PORT key FILE):
 *
 *     key                 reply "ok key check=<8 hex> sign_us=.. verify_us=.. forged=.. replayed=.." or "ok key none"
 *     key <32 hex>        save it and restart; reply "ok key check=<8 hex> (saved; restarting)"
 *
 * Neither reply ever holds the key. A transceiver without a key only listens (node_auth_ready is false).
 */
#ifndef NODE_AUTH_H
#define NODE_AUTH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "node_settings.h"

/* Load the key from NVS, check mbedTLS against a golden fixture tag and measure the cost per broadcast. Call once,
 * after nvs_flash_init. Returns node_auth_ready(). */
bool node_auth_start(void);

/* A kit key is loaded and mbedTLS passed the self-test: this node can sign and verify. */
bool node_auth_ready(void);

/* Write the tag of the round broadcast bytes[0..len) this node is about to send. False: do not send it. */
bool node_auth_sign(uint8_t *bytes, size_t len);

/* True if a heard round broadcast (decoded by rb_decode, heard at device time t_us) carries the kit's tag and a fresh
 * round number for its sender. Forged and replayed broadcasts are counted (see the key command) and give false. */
bool node_auth_accept(const uint8_t *bytes, size_t len, int64_t t_us);

/* HMAC-SHA256(kit key, msg) for values derived from the kit key (the OTA maintenance network passphrase and report
 * tags, node_ota.c). False without a key. Never the round broadcast tag: that is node_auth_sign. */
bool node_auth_mac(const uint8_t *msg, size_t len, uint8_t mac[32]);

/* Handle a "key ..." command line. Returns false (and touches nothing) for any other line; else fills reply and
 * sets *action to NODE_CMD_NONE, NODE_CMD_RESTART (the key was saved) or NODE_CMD_ERROR. */
bool node_auth_command(const char *line, char *reply, size_t n, node_cmd_t *action);

#endif /* NODE_AUTH_H */
