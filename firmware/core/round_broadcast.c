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

bool rb_carries_reports(uint8_t frame_type)
{
    return frame_type == RB_FRAME_ROUND || frame_type == RB_FRAME_ROUND_RAW;
}

static bool reporter(uint32_t membership, uint8_t sender, uint8_t index)
{
    return index < RB_MAX_NODES && index != sender && (membership >> index & 1u);
}

/* The raw CSI blocks filling data[off..len) exactly. */
static rb_status_t decode_raw(const uint8_t *data, size_t len, size_t off, rb_broadcast_t *out)
{
    uint8_t k = 0;
    while (off < len) {
        if (k == RB_RAW_MAX_LINKS || off + RB_RAW_HEAD_BYTES > len) {
            return RB_LENGTH;
        }
        uint8_t n = data[off + 4];
        if (n < 2 || n > RB_RAW_IQ_MAX || (n & 1u) || off + RB_RAW_HEAD_BYTES + n > len) {
            return RB_LENGTH;
        }
        if (!reporter(out->membership, out->node_index, data[off])) {
            return RB_MEMBERSHIP;
        }
        rb_raw_t *r = &out->raw[k++];
        r->transmitter_index = data[off];
        r->round = get_u16(data + off + 1);
        r->rssi = (int8_t)data[off + 3];
        r->n_iq = n;
        r->iq = (const int8_t *)(data + off + RB_RAW_HEAD_BYTES);
        off += RB_RAW_HEAD_BYTES + n;
    }
    out->n_raw = k;
    return k ? RB_OK : RB_LENGTH;
}

static rb_status_t decode_request(const uint8_t *data, size_t len, rb_request_t *q)
{
    if (len < RB_HEADER_BYTES + RB_REQUEST_HEAD_BYTES) {
        return RB_LENGTH;
    }
    const uint8_t *p = data + RB_HEADER_BYTES;
    uint8_t n = p[8];
    if (n > RB_RAW_MAX_LINKS || len != RB_HEADER_BYTES + RB_REQUEST_HEAD_BYTES + (size_t)n * RB_NODE_ID_BYTES) {
        return RB_LENGTH;
    }
    memcpy(q->target, p, RB_NODE_ID_BYTES);
    q->target_round = get_u16(p + 6);
    q->n = n;
    for (uint8_t k = 0; k < n; k++) {
        memcpy(q->transmitters[k], p + RB_REQUEST_HEAD_BYTES + (size_t)k * RB_NODE_ID_BYTES, RB_NODE_ID_BYTES);
    }
    return RB_OK;
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
    uint8_t frame = data[1];
    if (frame < RB_FRAME_ROUND || frame > RB_FRAME_ROUND_RAW) {
        return RB_FRAME_TYPE;
    }
    bool round = rb_carries_reports(frame);
    uint8_t index = data[7];
    uint32_t membership = get_u32(data + 8);
    if (frame != RB_FRAME_RAW_REQUEST
            && (index >= RB_MAX_NODES || (frame != RB_FRAME_JOIN_REQUEST && !(membership >> index & 1u)))) {
        return RB_MEMBERSHIP;
    }
    size_t base = round ? rb_size(membership) : RB_HEADER_BYTES;
    if (frame == RB_FRAME_ROUND_RAW ? len < base : (frame != RB_FRAME_RAW_REQUEST && len != base)) {
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
    out->len = (uint16_t)len;
    out->n_raw = 0;
    if (frame == RB_FRAME_RAW_REQUEST) {
        out->n_reports = 0;
        return decode_request(data, len, &out->request);
    }
    const uint8_t *p = data + RB_HEADER_BYTES;
    uint8_t k = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES && round; i++) {
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
        r->lateness = (uint8_t)(p[3] >> RB_LATENESS_SHIFT);
        p += RB_REPORT_BYTES;
    }
    out->n_reports = k;
    return frame == RB_FRAME_ROUND_RAW ? decode_raw(data, len, base, out) : RB_OK;
}

size_t rb_encode(const rb_broadcast_t *b, uint8_t *out, size_t cap)
{
    uint8_t frame = b->frame_type;
    bool round = rb_carries_reports(frame);
    if (frame < RB_FRAME_ROUND || frame > RB_FRAME_ROUND_RAW) {
        return 0;
    }
    if (frame != RB_FRAME_RAW_REQUEST && (b->node_index >= RB_MAX_NODES
            || (frame != RB_FRAME_JOIN_REQUEST && !(b->membership >> b->node_index & 1u)))) {
        return 0;
    }
    size_t n = round ? rb_size(b->membership) : RB_HEADER_BYTES;
    if (b->n_reports != (round ? members(b->membership) - 1 : 0)
            || (frame == RB_FRAME_ROUND_RAW ? (b->n_raw < 1 || b->n_raw > RB_RAW_MAX_LINKS) : b->n_raw != 0)) {
        return 0;
    }
    for (uint8_t k = 0; k < b->n_raw; k++) {
        const rb_raw_t *r = &b->raw[k];
        if (r->n_iq < 2 || r->n_iq > RB_RAW_IQ_MAX || (r->n_iq & 1u) || !r->iq
                || !reporter(b->membership, b->node_index, r->transmitter_index)) {
            return 0;
        }
        n += RB_RAW_HEAD_BYTES + r->n_iq;
    }
    if (frame == RB_FRAME_RAW_REQUEST) {
        if (b->request.n > RB_RAW_MAX_LINKS) {
            return 0;
        }
        n += RB_REQUEST_HEAD_BYTES + (size_t)b->request.n * RB_NODE_ID_BYTES;
    }
    if (n > cap) {
        return 0;
    }
    uint8_t k = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES && round; i++) {
        if (!(b->membership >> i & 1u) || i == b->node_index) {
            continue;
        }
        const rb_report_t *r = &b->reports[k++];
        if (r->transmitter_index != i || r->heard > RB_COUNT_MAX || r->since > RB_COUNT_MAX
                || r->flags > RB_FLAGS_KNOWN || r->lateness > RB_LATENESS_MAX) {
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
        p[3] = (uint8_t)(r->flags | r->lateness << RB_LATENESS_SHIFT);
        p += RB_REPORT_BYTES;
    }
    for (k = 0; k < b->n_raw; k++) {
        const rb_raw_t *r = &b->raw[k];
        p[0] = r->transmitter_index;
        put_u16(p + 1, r->round);
        p[3] = (uint8_t)r->rssi;
        p[4] = r->n_iq;
        memcpy(p + RB_RAW_HEAD_BYTES, r->iq, r->n_iq);
        p += RB_RAW_HEAD_BYTES + r->n_iq;
    }
    if (frame == RB_FRAME_RAW_REQUEST) {
        memcpy(p, b->request.target, RB_NODE_ID_BYTES);
        put_u16(p + 6, b->request.target_round);
        p[8] = b->request.n;
        for (k = 0; k < b->request.n; k++) {
            memcpy(p + RB_REQUEST_HEAD_BYTES + (size_t)k * RB_NODE_ID_BYTES, b->request.transmitters[k],
                   RB_NODE_ID_BYTES);
        }
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
