/*
 * Raw mode on a transceiver (ticket #17): which of its links stream full CSI, what it last heard on them, and the
 * raw CSI blocks its next round broadcast carries. docs/round-broadcast.md "Raw mode" defines the frames.
 *
 *   raw_mode_apply   a raw mode request (frame type 4) that passed the tag and freshness checks: if it names this
 *                    node and its target round is this node's own round number or up to RB_RAW_REQUEST_MAX_AGE
 *                    rounds before it, the named transmitters become the raw set (none: raw mode off). Each
 *                    request carries the whole set, so copies and repeats change nothing.
 *   raw_mode_heard   a round broadcast heard from a member: if its sender is in the raw set, keep its CSI (the
 *                    first RB_RAW_IQ_MAX bytes, as the driver gave them) until this node's next slot.
 *   raw_mode_take    in this node's slot: the raw CSI blocks to append, as many as fit in `room` bytes (the rest
 *                    are dropped and counted, never sent late); clears what it kept.
 *   raw_mode_flags   the node indices whose link reports get RB_FLAG_RAW_MODE: the raw set, as the scheduler names
 *                    its members. The flag is the host's acknowledgement.
 *
 * Transmitters are named by board serial, so the raw set survives a new schedule version. The set lives in RAM: a
 * restart turns raw mode off. Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef RAW_MODE_H
#define RAW_MODE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "round_broadcast.h"

typedef enum {
    RAW_APPLIED = 0,   /* the raw set is now the request's */
    RAW_NOT_MINE,      /* the request names another node */
    RAW_STALE,         /* its target round is not within this node's last RB_RAW_REQUEST_MAX_AGE rounds */
} raw_apply_t;

typedef struct {
    bool valid;
    uint8_t index;               /* the transmitter's node index when heard */
    uint16_t round;              /* its round number */
    int8_t rssi;
    uint8_t n_iq;
    int8_t iq[RB_RAW_IQ_MAX];
} raw_capture_t;

typedef struct {
    uint32_t applied;            /* requests applied */
    uint32_t ignored;            /* requests for another node, or stale */
    uint32_t sent;               /* raw CSI blocks sent */
    uint32_t skipped;            /* kept CSI that did not fit the slot, or whose transmitter left the round */
} raw_counters_t;

typedef struct {
    uint8_t n;                                       /* links in the raw set, 0..RB_RAW_MAX_LINKS */
    uint8_t ids[RB_RAW_MAX_LINKS][RB_NODE_ID_BYTES];  /* their transmitters' board serials */
    raw_capture_t kept[RB_RAW_MAX_LINKS];            /* kept[k]: the last CSI heard from ids[k] */
    raw_counters_t count;
} raw_mode_t;

void raw_mode_init(raw_mode_t *r);

/* A raw mode request heard by the node `my_id` whose latest frame carried round number `my_round`. */
raw_apply_t raw_mode_apply(raw_mode_t *r, const rb_request_t *q, const uint8_t my_id[RB_NODE_ID_BYTES],
                           uint16_t my_round);

/* A round broadcast from member `index` (serial `id`, round `round`) heard with this CSI. */
void raw_mode_heard(raw_mode_t *r, const uint8_t id[RB_NODE_ID_BYTES], uint8_t index, uint16_t round, int8_t rssi,
                    const int8_t *csi, size_t csi_len);

/* Fill out[] with the blocks for a round broadcast of `membership` sent by `my_index` with `room` bytes to spare;
 * returns how many. The I/Q points into r and stays valid until the next raw_mode_heard. */
uint8_t raw_mode_take(raw_mode_t *r, uint32_t membership, uint8_t my_index, size_t room,
                      rb_raw_t out[RB_RAW_MAX_LINKS]);

/* Bitmap of the member indices in the raw set: member i has serial member_id[i] when bit i of id_known is set. */
uint32_t raw_mode_flags(const raw_mode_t *r, const uint8_t member_id[RB_MAX_NODES][RB_NODE_ID_BYTES],
                        uint32_t id_known);

#endif /* RAW_MODE_H */
