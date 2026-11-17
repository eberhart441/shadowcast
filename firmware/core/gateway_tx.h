/*
 * The gateway's sending side (raw mode, ticket #17). A gateway listens; the one thing it sends is a frame the host
 * hands it over USB (`send <hex>`): a raw mode request the host built and signed. It sends it where the round
 * scheduler already lets a non-member speak, the join slot (the last slot of the round), and only in a round the
 * scheduler numbers even, where joiners send their requests: never in a member's slot, never in the odd rounds whose
 * join slot carries the scheduler's grants. A collision with a joiner costs both one try; the host sends each
 * request three times and the joiner backs off.
 *
 *   gt_heard  every decoded frame the gateway hears. Slot 0's round broadcast (its scheduler's) gives the round's
 *             start on air, its round number (parity) and its slots; two of them give the round period. With two
 *             schedules in range (before they merge) it follows the one with the lower scheduler ID, as the
 *             nodes will.
 *   gt_next   the device time to send the oldest queued frame: the join slot of the round whose slot 0 was just
 *             heard, if that round is even and nothing went out in it yet; -1 otherwise (wait for the next).
 *   gt_take   the slot timer fired: the frame to send now, or 0 when it is no longer due (more than GT_LATE_US
 *             late, or the schedule moved on). One frame per round.
 *
 * The round's start is taken as slot 0's start on air, so the gateway is never early: its frame goes out a send
 * latency or so into the join slot. Frames older than GT_STALE_US are dropped (the target ignores old requests
 * anyway, RB_RAW_REQUEST_MAX_AGE). Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef GATEWAY_TX_H
#define GATEWAY_TX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "round_broadcast.h"

#define GT_QUEUE 4                         /* frames waiting to go out */
#define GT_FRAME_MAX RB_REQUEST_MAX_BYTES  /* the longest frame the gateway sends: a raw mode request, 47 bytes */
#define GT_STALE_US 2000000                /* a frame not sent within this is dropped */
#define GT_SCHEDULE_STALE_US 1000000       /* a schedule whose slot 0 was not heard for this long is let go */
#define GT_LATE_US 250                     /* a slot timer this late sends nothing */
#define GT_EARLY_US 100                    /* nor one this early */

typedef struct {
    uint8_t len;
    uint8_t bytes[GT_FRAME_MAX];
    int64_t queued_us;
} gt_frame_t;

typedef struct {
    uint32_t sent;       /* frames sent */
    uint32_t dropped;    /* frames refused: queue full or too long */
    uint32_t stale;      /* frames dropped unsent after GT_STALE_US */
} gt_counters_t;

typedef struct {
    bool heard;                          /* a slot 0 was heard: the fields below are set */
    uint8_t sched_id[RB_NODE_ID_BYTES];
    int64_t round_start_us;              /* start on air of the last heard slot 0's round broadcast */
    uint16_t round;                      /* its round number (the scheduler's count) */
    uint8_t slots;                       /* its slots per round */
    int64_t period_us;                   /* measured round period; 0 until two slot 0s were heard */
    int64_t sent_start_us;               /* the round start of the last frame sent (one per round) */
    gt_frame_t q[GT_QUEUE];
    uint8_t n;
    gt_counters_t count;
} gt_t;

void gt_init(gt_t *g);

/* Queue a frame at device time now_us. False (counted) when the queue is full or the frame is too long. */
bool gt_queue(gt_t *g, const uint8_t *frame, size_t len, int64_t now_us);

/* A decoded frame heard at device time t_rx_us (the end of its reception). */
void gt_heard(gt_t *g, int64_t t_rx_us, const rb_broadcast_t *b);

/* When to send the oldest queued frame, or -1 (nothing queued, no schedule, an odd round, already sent in it). */
int64_t gt_next(gt_t *g, int64_t now_us);

/* The frame to send at now_us into out[0..cap) and its length; 0 when nothing is due. */
size_t gt_take(gt_t *g, int64_t now_us, uint8_t *out, size_t cap);

/* Parse the host's command line "send <hex>" into out[0..cap). False for any other line or bad hex. */
bool gt_parse_send(const char *line, uint8_t *out, size_t cap, size_t *len);

#endif /* GATEWAY_TX_H */
