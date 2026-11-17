#include "gateway_record.h"

#include <string.h>

uint16_t gr_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)(data[i] << 8);
        for (int b = 0; b < 8; b++) {
            crc = (uint16_t)(crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1);
        }
    }
    return crc;
}

static void put_u64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint64_t get_u64(const uint8_t *p)
{
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) {
        v = v << 8 | p[i];
    }
    return v;
}

static void put_u32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++) {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* Frame a body that is already at out + GR_HEAD_BYTES. */
static size_t frame(uint8_t *out, uint8_t type, size_t body_len)
{
    out[0] = GR_SYNC0;
    out[1] = GR_SYNC1;
    out[2] = GR_VERSION;
    out[3] = type;
    out[4] = (uint8_t)body_len;
    out[5] = (uint8_t)(body_len >> 8);
    uint16_t crc = gr_crc16(out + 2, 4 + body_len);
    out[GR_HEAD_BYTES + body_len] = (uint8_t)crc;
    out[GR_HEAD_BYTES + body_len + 1] = (uint8_t)(crc >> 8);
    return body_len + GR_FRAME_BYTES;
}

static int fits(size_t cap, size_t body_len)
{
    return body_len <= GR_MAX_BODY && body_len + GR_FRAME_BYTES <= cap;
}

size_t gr_encode_heard(uint8_t *out, size_t cap, uint64_t rx_time_us, int8_t rssi, const uint8_t *payload,
                       size_t payload_len)
{
    size_t body_len = GR_HEARD_HEAD + payload_len;
    if (!fits(cap, body_len)) {
        return 0;
    }
    uint8_t *body = out + GR_HEAD_BYTES;
    put_u64(body, rx_time_us);
    body[8] = (uint8_t)rssi;
    body[9] = 0;
    if (payload_len) {
        memcpy(body + GR_HEARD_HEAD, payload, payload_len);
    }
    return frame(out, GR_TYPE_HEARD, body_len);
}

size_t gr_encode_status(uint8_t *out, size_t cap, const gr_node_status_t *status)
{
    size_t body_len = GR_STATUS_HEAD + 4 * GR_STATUS_COUNTERS;
    if (!fits(cap, body_len)) {
        return 0;
    }
    uint8_t *body = out + GR_HEAD_BYTES;
    memcpy(body, status->serial, 6);
    put_u64(body + 6, status->uptime_us);
    body[14] = status->reset_reason;
    body[15] = status->role;
    body[16] = status->channel;
    body[17] = 0;
    for (int i = 0; i < GR_STATUS_COUNTERS; i++) {
        put_u32(body + GR_STATUS_HEAD + 4 * i, status->counters[i]);
    }
    return frame(out, GR_TYPE_STATUS, body_len);
}

size_t gr_encode_text(uint8_t *out, size_t cap, const char *text, size_t text_len)
{
    if (!fits(cap, text_len)) {
        return 0;
    }
    if (text_len) {
        memcpy(out + GR_HEAD_BYTES, text, text_len);
    }
    return frame(out, GR_TYPE_TEXT, text_len);
}

gr_status_t gr_decode(const uint8_t *data, size_t len, gr_record_t *out)
{
    if (len >= 1 && data[0] != GR_SYNC0) {
        return GR_SYNC;
    }
    if (len >= 2 && data[1] != GR_SYNC1) {
        return GR_SYNC;
    }
    if (len < GR_HEAD_BYTES) {
        return GR_TRUNCATED;
    }
    if (data[2] != GR_VERSION) {
        return GR_BAD_VERSION;
    }
    size_t body_len = (size_t)data[4] | (size_t)data[5] << 8;
    if (body_len > GR_MAX_BODY) {
        return GR_TOO_LONG;
    }
    if (len < body_len + GR_FRAME_BYTES) {
        return GR_TRUNCATED;
    }
    uint16_t crc = (uint16_t)(data[GR_HEAD_BYTES + body_len] | data[GR_HEAD_BYTES + body_len + 1] << 8);
    if (crc != gr_crc16(data + 2, 4 + body_len)) {
        return GR_CRC;
    }
    uint8_t type = data[3];
    size_t min_body;
    switch (type) {
    case GR_TYPE_HEARD: min_body = GR_HEARD_HEAD; break;
    case GR_TYPE_STATUS: min_body = GR_STATUS_HEAD; break;
    case GR_TYPE_TEXT: min_body = 0; break;
    default: return GR_TYPE;
    }
    if (body_len < min_body) {
        return GR_SHORT_BODY;
    }
    out->type = type;
    out->body = data + GR_HEAD_BYTES;
    out->body_len = (uint16_t)body_len;
    out->record_len = body_len + GR_FRAME_BYTES;
    return GR_OK;
}

void gr_heard_fields(const gr_record_t *r, uint64_t *rx_time_us, int8_t *rssi, const uint8_t **payload,
                     size_t *payload_len)
{
    *rx_time_us = get_u64(r->body);
    *rssi = (int8_t)r->body[8];
    *payload = r->body + GR_HEARD_HEAD;
    *payload_len = (size_t)r->body_len - GR_HEARD_HEAD;
}

void gr_status_fields(const gr_record_t *r, gr_node_status_t *status)
{
    memset(status, 0, sizeof *status);
    memcpy(status->serial, r->body, 6);
    status->uptime_us = get_u64(r->body + 6);
    status->reset_reason = r->body[14];
    status->role = r->body[15];
    status->channel = r->body[16];
    for (size_t i = 0; i < GR_STATUS_COUNTERS && GR_STATUS_HEAD + 4 * (i + 1) <= r->body_len; i++) {
        status->counters[i] = get_u32(r->body + GR_STATUS_HEAD + 4 * i);
    }
}

const char *gr_status_name(gr_status_t st)
{
    switch (st) {
    case GR_OK: return "ok";
    case GR_TRUNCATED: return "truncated";
    case GR_SYNC: return "sync";
    case GR_BAD_VERSION: return "version";
    case GR_TOO_LONG: return "too_long";
    case GR_CRC: return "crc";
    case GR_TYPE: return "type";
    case GR_SHORT_BODY: return "short_body";
    }
    return "unknown";
}
