#include "round_broadcast.h"

#include <string.h>

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static unsigned members(uint32_t membership)
{
    unsigned n = 0;
    for (; membership; membership &= membership - 1) {
        n++;
    }
    return n;
}

size_t rb_size(uint32_t membership)
{
    unsigned n = members(membership);
    return RB_HEADER_BYTES + RB_REPORT_BYTES * (size_t)(n ? n - 1 : 0);
}

rb_status_t rb_decode(const uint8_t *data, size_t len, rb_broadcast_t *out)
{
    if (len < RB_HEADER_BYTES) {
        return RB_SHORT;
    }
    if (data[0] != RB_VERSION) {
        return RB_BAD_VERSION;
    }
    if (data[1] != RB_FRAME_ROUND) {
        return RB_FRAME_TYPE;
    }
    uint8_t index = data[7];
    uint32_t membership = get_u32(data + 8);
    if (index >= RB_MAX_NODES || !(membership >> index & 1u)) {
        return RB_MEMBERSHIP;
    }
    if (len != rb_size(membership)) {
        return RB_LENGTH;
    }
    out->version = data[0];
    out->frame_type = data[1];
    out->round = get_u16(data + 2);
    out->slot = data[4];
    out->slots_per_round = data[5];
    out->schedule_version = data[6];
    out->node_index = index;
    out->membership = membership;
    memcpy(out->node_id, data + 12, RB_NODE_ID_BYTES);
    memcpy(out->tag, data + RB_TAG_OFFSET, RB_TAG_BYTES);
    const uint8_t *p = data + RB_HEADER_BYTES;
    uint8_t k = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if (!(membership >> i & 1u) || i == index) {
            continue;
        }
        rb_report_t *r = &out->reports[k++];
        r->transmitter_index = i;
        r->score = p[0];
        r->rssi = (int8_t)p[1];
        r->heard = (uint8_t)(p[2] >> 4);
        r->since = (uint8_t)(p[2] & 0x0F);
        r->flags = (uint8_t)(p[3] & RB_FLAGS_KNOWN);
        p += RB_REPORT_BYTES;
    }
    out->n_reports = k;
    return RB_OK;
}

size_t rb_encode(const rb_broadcast_t *b, uint8_t *out, size_t cap)
{
    if (b->node_index >= RB_MAX_NODES || !(b->membership >> b->node_index & 1u)) {
        return 0;
    }
    size_t n = rb_size(b->membership);
    if (n > cap || b->n_reports != members(b->membership) - 1) {
        return 0;
    }
    uint8_t k = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if (!(b->membership >> i & 1u) || i == b->node_index) {
            continue;
        }
        const rb_report_t *r = &b->reports[k++];
        if (r->transmitter_index != i || r->heard > RB_COUNT_MAX || r->since > RB_COUNT_MAX) {
            return 0;
        }
    }
    out[0] = b->version;
    out[1] = b->frame_type;
    put_u16(out + 2, b->round);
    out[4] = b->slot;
    out[5] = b->slots_per_round;
    out[6] = b->schedule_version;
    out[7] = b->node_index;
    put_u32(out + 8, b->membership);
    memcpy(out + 12, b->node_id, RB_NODE_ID_BYTES);
    memcpy(out + RB_TAG_OFFSET, b->tag, RB_TAG_BYTES);
    uint8_t *p = out + RB_HEADER_BYTES;
    for (k = 0; k < b->n_reports; k++) {
        const rb_report_t *r = &b->reports[k];
        p[0] = r->score;
        p[1] = (uint8_t)r->rssi;
        p[2] = (uint8_t)(r->heard << 4 | r->since);
        p[3] = r->flags;
        p += RB_REPORT_BYTES;
    }
    return n;
}

const char *rb_status_name(rb_status_t st)
{
    switch (st) {
    case RB_OK: return "ok";
    case RB_SHORT: return "short";
    case RB_BAD_VERSION: return "version";
    case RB_FRAME_TYPE: return "frame_type";
    case RB_LENGTH: return "length";
    case RB_MEMBERSHIP: return "membership";
    }
    return "unknown";
}
