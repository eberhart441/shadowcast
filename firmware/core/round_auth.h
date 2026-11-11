/*
 * Authenticated round broadcasts (format v2) on the node: write the tag on what it sends, check the tag and the
 * round number on what it hears. The definition is docs/round-broadcast.md "Authentication"; csi/round_auth.py is
 * the host's copy; both are pinned to tests/fixtures/round_broadcast_v2.json (tests/test_round_auth.c).
 *
 *   tag       the first 8 bytes of HMAC-SHA256(kit key, the broadcast with its tag bytes 18..25 set to zero)
 *   fresh     per sender (node id): the round number is ahead of the last fresh one from it by 1 to
 *             RB_AUTH_AHEAD_SLACK + 1 per millisecond since; or the sender has been silent for more than
 *             RB_AUTH_RESYNC_US (it restarted or renumbered). Anything else is a replay.
 *
 * The HMAC itself is the platform's: rb_auth_init takes a function that computes the full 32-byte HMAC-SHA256 of a
 * message under the kit key (on the node, mbedTLS with the SHA accelerator; in the PC tests, a portable reference).
 * The key never passes through this module.
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef ROUND_AUTH_H
#define ROUND_AUTH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "round_broadcast.h"

#define RB_AUTH_KEY_BYTES 16
#define RB_AUTH_MAC_BYTES 32          /* a full HMAC-SHA256; the tag is its first RB_TAG_BYTES */
#define RB_AUTH_CHECK_BYTES 4         /* the key check value: HMAC(key, "shadowcast kit key check"), first 4 bytes */
#define RB_AUTH_AHEAD_SLACK 16        /* rounds a fresh broadcast may be ahead, plus 1 per millisecond since */
#define RB_AUTH_RESYNC_US 2000000     /* a sender silent this long is taken at whatever round it sends next */
#define RB_AUTH_FRESH_NODES RB_MAX_NODES   /* senders remembered; a new one replaces the one heard longest ago */

/* HMAC-SHA256(kit key, msg[0..len)) into mac. Returns false if it could not be computed. */
typedef bool (*rb_auth_mac_fn)(void *ctx, const uint8_t *msg, size_t len, uint8_t mac[RB_AUTH_MAC_BYTES]);

typedef enum {
    RB_AUTH_OK = 0,
    RB_AUTH_FORGED,    /* the tag does not verify: forged, damaged, or another kit's key */
    RB_AUTH_REPLAY,    /* the tag verifies but the round number is stale for its sender */
} rb_auth_status_t;

typedef struct {
    uint8_t node_id[RB_NODE_ID_BYTES];
    uint16_t round;     /* the last fresh round from this sender */
    int64_t t_us;       /* when it was heard */
    bool used;
} rb_auth_sender_t;

typedef struct {
    rb_auth_mac_fn mac;
    void *ctx;
    rb_auth_sender_t senders[RB_AUTH_FRESH_NODES];
} rb_auth_t;

void rb_auth_init(rb_auth_t *a, rb_auth_mac_fn mac, void *ctx);

/* Write the tag of bytes[0..len) (a whole round broadcast, at most RB_MAX_BYTES) into its tag field. */
bool rb_auth_sign(const rb_auth_t *a, uint8_t *bytes, size_t len);

/* True if bytes[0..len) carries its tag. Constant time in the tag. False for fewer than RB_HEADER_BYTES. */
bool rb_auth_tag_ok(const rb_auth_t *a, const uint8_t *bytes, size_t len);

/* The freshness rule for one authenticated broadcast from node_id with `round`, heard at device time t_us.
 * RB_AUTH_OK (remembered as the sender's last fresh round) or RB_AUTH_REPLAY (nothing changes). */
rb_auth_status_t rb_auth_fresh(rb_auth_t *a, const uint8_t node_id[RB_NODE_ID_BYTES], uint16_t round, int64_t t_us);

/* Both, on a heard round broadcast that decoded (rb_decode): the tag, then the freshness of its node id and round.
 * A forged broadcast never touches the freshness state. */
rb_auth_status_t rb_auth_check(rb_auth_t *a, const uint8_t *bytes, size_t len, int64_t t_us);

/* The key check value, which names the key without revealing it (scripts/node.py and the dashboard show it). */
bool rb_auth_key_check(const rb_auth_t *a, uint8_t out[RB_AUTH_CHECK_BYTES]);

/* Parse a kit key written as 32 hex digits (scripts/node.py PORT key). Leaves key unchanged on failure. */
bool rb_auth_parse_key(const char *hex, uint8_t key[RB_AUTH_KEY_BYTES]);

/* True for the published test key of the fixtures and the simulator (00 01 .. 0f), which no kit may use. */
bool rb_auth_is_test_key(const uint8_t key[RB_AUTH_KEY_BYTES]);

/* "ok", "auth" or "replay": the host's rejection reasons. */
const char *rb_auth_status_name(rb_auth_status_t st);

#endif /* ROUND_AUTH_H */
