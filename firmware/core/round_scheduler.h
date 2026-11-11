/*
 * The round scheduler (ticket #14): who is in the round, which slot is whose, and when this node sends. It replaces
 * the fixed round of #13 behind the same kind of calls. Heard round broadcasts and the device clock go in; this
 * node's next transmission, its node index, the membership and the schedule version come out. There is no timing
 * master and no per-board setting: every node runs the same rules (docs/adr/0027-round-scheduler.md).
 *
 *   Round       round_hz rounds per second. A round has one slot per member, in ascending node index, then the
 *               join slot; all slots are equally long (period / slots per round).
 *   Timing      from any round broadcast of this node's schedule: it implies when that round started on this
 *               node's clock (receive time - airtime - this node's own send latency - slot x slot length). The
 *               first one sets the phase outright, later ones move it halfway.
 *   Numbers     every node numbers its own frames: one up every round, never back, across schedule versions,
 *               joins and merges. Round numbers are not shared between nodes (the host keys everything by
 *               sender), so the freshness rule of authenticated broadcasts (ticket #16: 1 to 16 + 1/ms ahead of
 *               the sender's last) always holds, except after a power cycle.
 *   Scheduler   the member in slot 0. Only it admits joiners and drops members, by bumping the schedule version;
 *               every other member takes a newer schedule from any member's round broadcast or from a join grant.
 *               Its board serial is the schedule's scheduler ID.
 *   Joining     a node that powers on listens. When it hears a schedule it syncs to it and, once it has heard
 *               slot 0, sends a join request in the join slot of a round the scheduler numbers even (random
 *               backoff, doubling after each try); the scheduler answers with a join grant naming the joiner's
 *               serial and its node index in the join slot of the next round it numbers odd. Both join frames carry
 *               the joiner's round numbers. A node that hears nothing for 3 to 7 rounds founds its own schedule,
 *               alone in slot 0.
 *   Dropping    a member nobody has heard for RS_MISSING_ROUNDS rounds (directly, or by anyone's link reports)
 *               is dropped by the scheduler. When the scheduler itself is missing, the next member in slot order
 *               whose earlier members are all missing drops them and becomes slot 0 (failover).
 *   Merging     a node that hears slot 0 of another schedule whose scheduler ID (serial) is lower than its own
 *               schedule's leaves its schedule and joins that one. So split groups end in the lowest-ID schedule.
 *   Guard       a slot must hold the round broadcast's airtime plus the guard; the scheduler admits no more
 *               members than that allows, and a node whose slot came more than the guard late skips it.
 *   Lateness    for each round broadcast heard from a member: how far into its slot it started on air, by this
 *               node's schedule. Sent to the host in the link report (docs/round-broadcast.md).
 *
 * Never two different round broadcasts with one round number in one schedule version (the host drops the second
 * as a duplicate, docs/round-broadcast.md): rs_next never plans a frame at or before the last round number this
 * node sent, in any schedule version.
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef ROUND_SCHEDULER_H
#define ROUND_SCHEDULER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "round_broadcast.h"

#define RS_MISSING_ROUNDS 3        /* a member not heard for this many rounds is dropped */
#define RS_GUARD_DEFAULT_US 250    /* the slot guard until the slot-jitter bench test sets it */
#define RS_MIN_SLOT_US 1000        /* the shortest slot the scheduler makes */
#define RS_NO_INDEX 0xFF
#define RS_LATENESS_UNIT_US 32     /* lateness code c (1..31): [(c - 1) x 32, c x 32) us; 31 is 960 us or more */
#define RS_LATENESS_NONE 0         /* no round broadcast from that member measured since the last report */
#define RS_LATENESS_MAX_CODE 31

typedef enum {
    RS_LISTENING = 0,   /* heard no schedule yet: sends nothing */
    RS_JOINING,         /* synced to a schedule, asking to join it: sends only join requests */
    RS_MEMBER,          /* has a node index and a slot: sends a round broadcast every round */
} rs_state_t;

typedef enum {
    RS_TX_NONE = 0,
    RS_TX_ROUND,          /* this node's round broadcast */
    RS_TX_JOIN_REQUEST,   /* a join request in the join slot */
    RS_TX_JOIN_GRANT,     /* the scheduler's join grant in the join slot */
} rs_tx_kind_t;

typedef struct {
    rs_tx_kind_t kind;
    int64_t at_us;        /* device time to send at */
    uint16_t round;       /* the round number it belongs to */
} rs_tx_t;

/* What rs_heard made of a frame. */
typedef enum {
    RS_HEARD_IGNORED = 0,   /* not used: own frame, malformed slot fields, another schedule, or a join frame */
    RS_HEARD_MEMBER,        /* a round broadcast from a member of this node's schedule: score its link */
    RS_HEARD_OTHER,         /* used (timing or schedule), but not from a member of this node's schedule */
} rs_heard_t;

typedef struct {
    uint32_t joins;          /* times this node became a member (founding included) */
    uint32_t founded;        /* schedules this node founded alone */
    uint32_t admitted;       /* join grants sent (as scheduler) */
    uint32_t dropped;        /* members dropped (as scheduler, failover included) */
    uint32_t merges;         /* times this node left its schedule for one with a lower scheduler ID */
    uint32_t evicted;        /* times this node found itself dropped from its schedule */
    uint32_t requests;       /* join requests sent */
    uint32_t late_slots;     /* slots skipped because they came more than the guard late */
    uint32_t foreign;        /* round broadcasts from another schedule */
    uint32_t refused;        /* join requests refused: the round is full */
} rs_counters_t;

typedef struct {
    /* settings */
    uint8_t id[RB_NODE_ID_BYTES];
    int64_t period_us;
    int64_t guard_us;
    uint8_t max_members;
    uint32_t rng;

    rs_state_t state;
    /* the schedule this node is in (MEMBER) or is joining (JOINING) */
    uint8_t version;
    uint32_t membership;
    uint8_t my_index;                         /* RS_NO_INDEX unless MEMBER */
    uint8_t member_id[RB_MAX_NODES][RB_NODE_ID_BYTES];
    uint32_t id_known;                        /* bit i: member_id[i] is known */
    int64_t seen_us[RB_MAX_NODES];            /* latest evidence that member i is alive */
    uint8_t sched_id[RB_NODE_ID_BYTES];       /* the serial of slot 0's member */
    bool sched_known;

    /* timing */
    bool synced;
    int64_t base_us;                          /* device time at which round base_round starts */
    uint16_t base_round;
    int64_t latency_us;                       /* this node's own send latency (slot start to start on air) */
    bool latency_known;
    int64_t last_related_us;                  /* last frame heard from this node's schedule */
    int64_t listen_until_us;                  /* LISTENING: found a schedule at this time */

    /* joining */
    uint16_t next_request_round;
    uint8_t backoff_window;

    /* scheduler: one join grant pending */
    bool grant_pending;
    uint8_t grant_id[RB_NODE_ID_BYTES];
    uint8_t grant_index;
    int64_t grant_by_us;                      /* forget the request after this */
    uint16_t grant_round;                     /* the request's round number (the joiner's count) */
    int64_t grant_k;                          /* and the round (from base) it was heard in */

    /* round numbers: this node's own count */
    bool sent_any;
    uint16_t last_sent_round;                 /* of the last frame carrying this node's serial that it sent */
    int16_t sched_offset;                     /* the scheduler's round number minus this node's, in one round */
    bool sched_offset_known;
    int64_t last_k;                           /* timing: the round (from base) the last heard broadcast fell in */

    uint8_t lateness[RB_MAX_NODES];           /* lateness code per member, since the last report */
    uint32_t restarts;                        /* links whose node index now means another node */
    rs_counters_t count;
} rs_t;

/* Start listening at now_us. `id` is this node's board serial, `seed` any per-board value (its serial bytes do).
 * Returns false (and leaves s unusable) if round_hz and guard_us cannot make a round of two members. */
bool rs_init(rs_t *s, const uint8_t id[RB_NODE_ID_BYTES], uint32_t round_hz, uint32_t guard_us, uint32_t seed,
             int64_t now_us);

/* A decoded frame (round broadcast or join frame) from another node, heard at device time t_rx_us. */
rs_heard_t rs_heard(rs_t *s, int64_t t_rx_us, const rb_broadcast_t *b);

/* The next transmission strictly after after_us (call it with about the current time). kind RS_TX_NONE never
 * happens: a listening node is told when it will found its own schedule. Call it again after every event. */
rs_tx_t rs_next(rs_t *s, int64_t after_us);

/* The planned transmission tx is due at now_us. Returns true and fills the header of *out (no link reports:
 * for a round broadcast the caller adds one per member of out->membership other than out->node_index) when it
 * is still to be sent; false when the plan changed or the slot is more than the guard late. */
bool rs_take(rs_t *s, const rs_tx_t *tx, int64_t now_us, rb_broadcast_t *out);

/* This node's frame of `bytes` bytes, planned for t_slot_us, was sent (send callback) at t_done_us. */
void rs_sent(rs_t *s, int64_t t_slot_us, int64_t t_done_us, size_t bytes);

/* The lateness code of member `index`'s last round broadcast heard since the previous call (RS_LATENESS_NONE if
 * none), for its link report; clears it. */
uint8_t rs_take_lateness(rs_t *s, uint8_t index);

/* Bitmap of node indices whose links must start over (the index now names another node); clears it. */
uint32_t rs_take_restarts(rs_t *s);

/* The slot of node index `index` in a round with this membership: its rank among the members. */
uint8_t rs_slot_of(uint32_t membership, uint8_t index);

/* Members allowed in a round at round_hz with guard_us (0 if not even 2 fit). */
uint8_t rs_max_members(uint32_t round_hz, uint32_t guard_us);

/* Time on air of an ESP-NOW broadcast with `bytes` of payload at HT20 MCS0. */
int64_t rs_airtime_us(size_t bytes);

/* The lateness code of a broadcast that started late_us into its slot. */
uint8_t rs_lateness_code(int64_t late_us);

#endif /* ROUND_SCHEDULER_H */
