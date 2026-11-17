#include "round_auth.h"

#include <string.h>

static const char KEY_CHECK_MESSAGE[] = "shadowcast kit key check";

void rb_auth_init(rb_auth_t *a, rb_auth_mac_fn mac, void *ctx)
{
    memset(a, 0, sizeof *a);
    a->mac = mac;
    a->ctx = ctx;
}

/* The tag of bytes[0..len): the MAC of a copy with the tag field zeroed. */
static bool tag_of(const rb_auth_t *a, const uint8_t *bytes, size_t len, uint8_t tag[RB_TAG_BYTES])
{
    uint8_t msg[RB_MAX_FRAME_BYTES], mac[RB_AUTH_MAC_BYTES];
    if (len < RB_HEADER_BYTES || len > RB_MAX_FRAME_BYTES || !a->mac) {
        return false;
    }
    memcpy(msg, bytes, len);
    memset(msg + RB_TAG_OFFSET, 0, RB_TAG_BYTES);
    if (!a->mac(a->ctx, msg, len, mac)) {
        return false;
    }
    memcpy(tag, mac, RB_TAG_BYTES);
    return true;
}

bool rb_auth_sign(const rb_auth_t *a, uint8_t *bytes, size_t len)
{
    uint8_t tag[RB_TAG_BYTES];
    if (!tag_of(a, bytes, len, tag)) {
        return false;
    }
    memcpy(bytes + RB_TAG_OFFSET, tag, RB_TAG_BYTES);
    return true;
}

bool rb_auth_tag_ok(const rb_auth_t *a, const uint8_t *bytes, size_t len)
{
    uint8_t tag[RB_TAG_BYTES];
    if (!tag_of(a, bytes, len, tag)) {
        return false;
    }
    uint8_t diff = 0;
    for (int i = 0; i < RB_TAG_BYTES; i++) {
        diff |= (uint8_t)(tag[i] ^ bytes[RB_TAG_OFFSET + i]);
    }
    return diff == 0;
}

rb_auth_status_t rb_auth_fresh(rb_auth_t *a, const uint8_t node_id[RB_NODE_ID_BYTES], uint16_t round, int64_t t_us)
{
    rb_auth_sender_t *slot = NULL, *oldest = NULL;
    for (int i = 0; i < RB_AUTH_FRESH_NODES; i++) {
        rb_auth_sender_t *s = &a->senders[i];
        if (s->used && memcmp(s->node_id, node_id, RB_NODE_ID_BYTES) == 0) {
            slot = s;
            break;
        }
        if (!oldest || !s->used || (oldest->used && s->t_us < oldest->t_us)) {
            oldest = s;
        }
    }
    if (slot) {
        int64_t dt = t_us - slot->t_us;
        if (dt <= RB_AUTH_RESYNC_US) {
            int ahead = (int)(uint16_t)(round - slot->round);
            if (ahead >= 32768) {
                ahead -= 65536;
            }
            int64_t window = RB_AUTH_AHEAD_SLACK + (dt > 0 ? dt / 1000 : 0);
            if (ahead < 1 || ahead > window) {
                return RB_AUTH_STALE;
            }
        }
    } else {
        slot = oldest;
        memcpy(slot->node_id, node_id, RB_NODE_ID_BYTES);
        slot->used = true;
    }
    slot->round = round;
    slot->t_us = t_us;
    return RB_AUTH_OK;
}

rb_auth_status_t rb_auth_check(rb_auth_t *a, const uint8_t *bytes, size_t len, int64_t t_us)
{
    if (!rb_auth_tag_ok(a, bytes, len)) {
        return RB_AUTH_FORGED;
    }
    uint16_t round = (uint16_t)(bytes[2] | bytes[3] << 8);
    return rb_auth_fresh(a, bytes + 12, round, t_us);
}

bool rb_auth_key_check(const rb_auth_t *a, uint8_t out[RB_AUTH_CHECK_BYTES])
{
    uint8_t mac[RB_AUTH_MAC_BYTES];
    if (!a->mac || !a->mac(a->ctx, (const uint8_t *)KEY_CHECK_MESSAGE, sizeof KEY_CHECK_MESSAGE - 1, mac)) {
        return false;
    }
    memcpy(out, mac, RB_AUTH_CHECK_BYTES);
    return true;
}

static int nibble(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

bool rb_auth_parse_key(const char *hex, uint8_t key[RB_AUTH_KEY_BYTES])
{
    uint8_t out[RB_AUTH_KEY_BYTES];
    for (int i = 0; i < RB_AUTH_KEY_BYTES; i++) {
        int hi = nibble(hex[2 * i]);
        int lo = hi < 0 ? -1 : nibble(hex[2 * i + 1]);
        if (lo < 0) {
            return false;   /* also stops at the end of a short string: '\0' is not a digit */
        }
        out[i] = (uint8_t)(hi << 4 | lo);
    }
    if (hex[2 * RB_AUTH_KEY_BYTES] != '\0') {
        return false;
    }
    memcpy(key, out, sizeof out);
    return true;
}

bool rb_auth_is_test_key(const uint8_t key[RB_AUTH_KEY_BYTES])
{
    for (int i = 0; i < RB_AUTH_KEY_BYTES; i++) {
        if (key[i] != i) {
            return false;
        }
    }
    return true;
}

const char *rb_auth_status_name(rb_auth_status_t st)
{
    switch (st) {
    case RB_AUTH_OK: return "ok";
    case RB_AUTH_FORGED: return "auth";
    case RB_AUTH_STALE: return "stale";
    }
    return "unknown";
}
