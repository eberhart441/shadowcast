/*
 * Round broadcast format v2 on the node: encode the bytes a transceiver sends in its slot, decode the ones it hears.
 *
 * The one definition of the format is docs/round-broadcast.md; csi/broadcast.py is the host's copy. Both are pinned
 * to the golden fixtures in tests/fixtures/round_broadcast_v2.json, which tests/test_round_broadcast.c replays.
 * v2 has v1's layout; its authentication tag is real (round_auth.h writes and checks it). This codec carries the
 * tag bytes as they are.
 * A 26-byte round header, then one 4-byte link report per member other than the sender, in ascending node index.
 * Little-endian throughout. Change the doc, the fixtures, csi/broadcast.py and this module together.
 *
 * Raw mode (ticket #17): a round broadcast with raw CSI (frame type 5) has 1 or 2 raw CSI blocks after its link
 * reports; a raw mode request (frame type 4, sent by a gateway, signed by the host) is the header, then the target
 * node, its round number and 0 to 2 transmitter node ids. The decoder points raw blocks' I/Q into the decoded bytes.
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef ROUND_BROADCAST_H
#define ROUND_BROADCAST_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RB_VERSION 2               /* v1 had the same layout with an unchecked, zero tag */
#define RB_FRAME_ROUND 1          /* a round broadcast carrying link reports */
#define RB_FRAME_JOIN_REQUEST 2   /* a node asking to join, in the join slot: the header alone */
#define RB_FRAME_JOIN_GRANT 3     /* the scheduler admitting it, in the join slot: the header alone */
#define RB_FRAME_RAW_REQUEST 4    /* the host asking a node to stream raw CSI (sent by a gateway in the join slot) */
#define RB_FRAME_ROUND_RAW 5      /* a round broadcast with raw CSI blocks after its link reports */
#define RB_MAX_NODES 32           /* the membership bitmap is 32 bits */
#define RB_HEADER_BYTES 26
#define RB_REPORT_BYTES 4
#define RB_NODE_ID_BYTES 6
#define RB_TAG_OFFSET 18
#define RB_TAG_BYTES 8
#define RB_MAX_REPORTS (RB_MAX_NODES - 1)
#define RB_MAX_BYTES (RB_HEADER_BYTES + RB_REPORT_BYTES * RB_MAX_REPORTS)   /* 150 */
#define RB_RAW_MAX_LINKS 2        /* raw CSI blocks in one round broadcast */
#define RB_RAW_HEAD_BYTES 5       /* transmitter index, its round number u16, rssi, I/Q byte count */
#define RB_RAW_IQ_MAX 128         /* 64 subcarriers x (imaginary, real): one HT20 long training field */
#define RB_RAW_BLOCK_MAX (RB_RAW_HEAD_BYTES + RB_RAW_IQ_MAX)                 /* 133 */
#define RB_MAX_FRAME_BYTES (RB_MAX_BYTES + RB_RAW_MAX_LINKS * RB_RAW_BLOCK_MAX)   /* 416: any frame of ours */
#define RB_REQUEST_HEAD_BYTES 9   /* target node id, target round u16, number of transmitters */
#define RB_REQUEST_MAX_BYTES (RB_HEADER_BYTES + RB_REQUEST_HEAD_BYTES + RB_RAW_MAX_LINKS * RB_NODE_ID_BYTES)  /* 47 */
#define RB_RAW_REQUEST_MAX_AGE 64 /* a node takes a request only within this many rounds after its target round */
#define RB_RSSI_NONE (-128)       /* the rssi byte when the transmitter was never heard */
#define RB_COUNT_MAX 15           /* heard and since are 4-bit; since 15 means 15 or more rounds, or never */

#define RB_FLAG_WARMING 0x01       /* the receiving node's score on this link is still warming up */
#define RB_FLAG_POSSIBLE_MOVE 0x02 /* the receiving node suspects a node moved */
#define RB_FLAG_RAW_MODE 0x04      /* raw mode is active on this link */
#define RB_FLAGS_KNOWN 0x07        /* bits 0-2 of byte 3; bits 3-7 are the slot lateness code */
#define RB_LATENESS_SHIFT 3
#define RB_LATENESS_MAX 31
#define RB_LATENESS_NONE_CODE 0   /* not measured */

typedef enum {
    RB_OK = 0,
    RB_SHORT,        /* shorter than the round header */
    RB_BAD_VERSION,  /* not format version 2 */
    RB_FRAME_TYPE,   /* a frame type v2 does not define */
    RB_LENGTH,       /* the length does not match the membership */
    RB_MEMBERSHIP,   /* the sender's node index is not in the membership */
} rb_status_t;

/* What the sender measured on one link: itself receiving from transmitter_index. */
typedef struct {
    uint8_t transmitter_index;  /* not in the bytes: from the membership */
    uint8_t score;              /* score code (motion_score_encode); 0 = no score yet */
    int8_t rssi;                /* dBm of the last round broadcast heard from it; RB_RSSI_NONE if never heard */
    uint8_t heard;              /* round broadcasts heard from it over the last 15 rounds, 0..15 */
    uint8_t since;              /* rounds since it was last heard, 0..15 */
    uint8_t flags;              /* RB_FLAG_* */
    uint8_t lateness;           /* slot lateness code of the transmitter's broadcast, 0..31; 0 = not measured */
} rb_report_t;

/* One raw CSI block: the sender's CSI of the last round broadcast it heard from transmitter_index. */
typedef struct {
    uint8_t transmitter_index;  /* a member other than the sender */
    uint16_t round;             /* the transmitter's round number of that round broadcast */
    int8_t rssi;                /* its dBm; RB_RSSI_NONE if not known */
    uint8_t n_iq;               /* I/Q bytes: even, 2..RB_RAW_IQ_MAX */
    const int8_t *iq;           /* imaginary, real per subcarrier, as the driver gave them (decode: into the bytes) */
} rb_raw_t;

/* The body of a raw mode request: `target` streams raw CSI for its links from `transmitters` (none: raw mode off). */
typedef struct {
    uint8_t target[RB_NODE_ID_BYTES];
    uint16_t target_round;      /* the target's own round number when the host asked */
    uint8_t n;                  /* 0..RB_RAW_MAX_LINKS */
    uint8_t transmitters[RB_RAW_MAX_LINKS][RB_NODE_ID_BYTES];
} rb_request_t;

typedef struct {
    uint8_t version;
    uint8_t frame_type;
    uint16_t round;             /* wraps */
    uint8_t slot;
    uint8_t slots_per_round;    /* including the join slot */
    uint8_t schedule_version;
    uint8_t node_index;         /* the sender's index, 0..31 */
    uint32_t membership;        /* bit i set when node index i is a member */
    uint8_t node_id[RB_NODE_ID_BYTES];  /* the sender's board serial (base MAC), transmission order */
    uint8_t tag[RB_TAG_BYTES];          /* authentication tag (round_auth.h) */
    uint8_t n_reports;
    rb_report_t reports[RB_MAX_REPORTS];
    uint8_t n_raw;                      /* RB_FRAME_ROUND_RAW: 1..RB_RAW_MAX_LINKS; else 0 */
    rb_raw_t raw[RB_RAW_MAX_LINKS];
    rb_request_t request;               /* RB_FRAME_RAW_REQUEST only */
    uint16_t len;                       /* rb_decode: the frame's length in bytes (its airtime); 0 when built */
} rb_broadcast_t;

/* Decode `len` bytes into *out: a round broadcast (with raw CSI or without), a join request or grant (n_reports 0;
 * in a request the node index is the index asked for, in a grant the index given, which must be in the new
 * membership) or a raw mode request (n_reports 0; its header fields after the round are not checked). Raw blocks
 * point into data, which must outlive *out. Anything but RB_OK leaves *out unspecified. */
rb_status_t rb_decode(const uint8_t *data, size_t len, rb_broadcast_t *out);

/* Encode *b into out[0..cap). Returns the byte count, or 0 when b is not a valid v2 broadcast (the sender is not a
 * member, the reports do not follow the membership, a count is over 15, flags or lateness out of range, a join
 * frame with reports) or does not fit in cap. */
size_t rb_encode(const rb_broadcast_t *b, uint8_t *out, size_t cap);

/* The byte count of a broadcast with this membership: 26 + 4 x (members - 1). */
size_t rb_size(uint32_t membership);

/* A node's round broadcast (frame type 1 or 5), as opposed to a join frame or a raw mode request. */
bool rb_carries_reports(uint8_t frame_type);

/* The host's name for a status, as the rejection counters use it: "ok", "short", "version", "frame_type", "length",
 * "membership". */
const char *rb_status_name(rb_status_t st);

#endif /* ROUND_BROADCAST_H */
