/*
 * Gateway record format v1: the framed binary records a node writes to its USB port (docs/gateway-record.md).
 *
 * A gateway writes one heard record per ESP-NOW frame it receives (its receive time, RSSI and the frame's body,
 * a round broadcast for our nodes). Every node writes a status record once a second and text records for settings
 * replies and log lines. Each record is framed: sync "SC", version, type, u16 body length, body, CRC-16/CCITT-FALSE
 * over version..body. The host's reader (csi/gateway_records.py) resynchronises on the sync bytes, so a corrupt
 * record or boot text costs only itself. Pinned to tests/fixtures/gateway_record_v1.json.
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef GATEWAY_RECORD_H
#define GATEWAY_RECORD_H

#include <stddef.h>
#include <stdint.h>

#define GR_SYNC0 0x53              /* 'S' */
#define GR_SYNC1 0x43              /* 'C' */
#define GR_VERSION 1
#define GR_TYPE_HEARD 1
#define GR_TYPE_STATUS 2
#define GR_TYPE_TEXT 3
#define GR_HEAD_BYTES 6            /* sync, version, type, length */
#define GR_FRAME_BYTES 8           /* head plus CRC */
#define GR_MAX_BODY 1500
#define GR_MAX_RECORD (GR_MAX_BODY + GR_FRAME_BYTES)
#define GR_HEARD_HEAD 10           /* receive time u64, rssi i8, reserved */
#define GR_STATUS_HEAD 18          /* serial, uptime u64, reset reason, role, channel, reserved */
#define GR_ROLE_TRANSCEIVER 0
#define GR_ROLE_GATEWAY 1

/* Status counters, in record order (docs/gateway-record.md). Counted since boot, never reset. */
enum {
    GR_COUNT_HEARD = 0,
    GR_COUNT_QUEUE_FULL,
    GR_COUNT_USB_DROPPED,
    GR_COUNT_SENT,
    GR_COUNT_SEND_ERRORS,
    GR_COUNT_REJECTED,
    GR_COUNT_CSI_SKIPPED,
    GR_STATUS_COUNTERS
};

typedef enum {
    GR_OK = 0,
    GR_TRUNCATED,    /* fewer bytes than the record needs */
    GR_SYNC,         /* does not start with the sync bytes */
    GR_BAD_VERSION,
    GR_TOO_LONG,     /* body length over GR_MAX_BODY */
    GR_CRC,
    GR_TYPE,         /* a type v1 does not define */
    GR_SHORT_BODY,   /* a body too short for its type */
} gr_status_t;

typedef struct {
    uint8_t serial[6];
    uint64_t uptime_us;
    uint8_t reset_reason;
    uint8_t role;        /* GR_ROLE_* */
    uint8_t channel;
    uint32_t counters[GR_STATUS_COUNTERS];
} gr_node_status_t;

/* One decoded record. body points into the decoded buffer. */
typedef struct {
    uint8_t type;
    const uint8_t *body;
    uint16_t body_len;
    size_t record_len;   /* bytes the whole record took, framing included */
} gr_record_t;

/* CRC-16/CCITT-FALSE: polynomial 0x1021, initial value 0xFFFF. */
uint16_t gr_crc16(const uint8_t *data, size_t len);

/* Each encoder writes one record into out[0..cap) and returns its length, or 0 when it does not fit or the body
 * would be over GR_MAX_BODY. */
size_t gr_encode_heard(uint8_t *out, size_t cap, uint64_t rx_time_us, int8_t rssi, const uint8_t *payload,
                       size_t payload_len);
size_t gr_encode_status(uint8_t *out, size_t cap, const gr_node_status_t *status);
size_t gr_encode_text(uint8_t *out, size_t cap, const char *text, size_t text_len);

/* Decode the record at the start of data[0..len). */
gr_status_t gr_decode(const uint8_t *data, size_t len, gr_record_t *out);

/* The fields of a decoded heard or status record (the record must be of that type and decoded with GR_OK). */
void gr_heard_fields(const gr_record_t *r, uint64_t *rx_time_us, int8_t *rssi, const uint8_t **payload,
                     size_t *payload_len);
void gr_status_fields(const gr_record_t *r, gr_node_status_t *status);

/* The reader's name for a status: "ok", "truncated", "sync", "version", "too_long", "crc", "type", "short_body". */
const char *gr_status_name(gr_status_t st);

#endif /* GATEWAY_RECORD_H */
