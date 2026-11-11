#include "round_scheduler.h"

#include <string.h>

#define LISTEN_MIN_ROUNDS 3        /* a node that hears nothing founds its own schedule after 3 to 7 rounds */
#define LISTEN_SPREAD_ROUNDS 4
#define LONE_RESHUFFLE_ROUNDS 16   /* a lone member that hears nothing moves its phase (two founders in step) */
#define BACKOFF_START 2            /* join requests: pick one of the next 2 request slots, doubling after each */
#define BACKOFF_MAX 16
#define GRANT_KEEP_ROUNDS 4        /* a request not granted within this many rounds is forgotten */
#define ESPNOW_OVERHEAD_BYTES 43   /* MAC header 24 + vendor action and element 15 + FCS 4 */
#define ALL_INDICES 0xFFFFFFFFu

/* ---------------------------------------------------------------- small helpers */

static uint32_t rnd(rs_t *s)
{
    uint32_t x = s->rng;   /* xorshift32 */
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    s->rng = x;
    return x;
}

/* floor(a / b) for b > 0 */
static int64_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    return (a % b != 0 && a < 0) ? q - 1 : q;
}

static unsigned members(uint32_t m)
{
    unsigned n = 0;
    for (; m; m &= m - 1) {
        n++;
    }
    return n;
}

static uint32_t bit(uint8_t index)
{
    return index < RB_MAX_NODES ? 1u << index : 0;
}

static bool same_id(const uint8_t *a, const uint8_t *b)
{
    return memcmp(a, b, RB_NODE_ID_BYTES) == 0;
}

/* a is a newer schedule version than b (u8 serial arithmetic) */
static bool newer(uint8_t a, uint8_t b)
{
    return (int8_t)(uint8_t)(a - b) > 0;
}

static size_t broadcast_bytes(uint32_t membership)
{
    unsigned n = members(membership);
    return RB_HEADER_BYTES + RB_REPORT_BYTES * (size_t)(n ? n - 1 : 0);
}

uint8_t rs_slot_of(uint32_t membership, uint8_t index)
{
    return (uint8_t)members(membership & (bit(index) - 1u));
}

int64_t rs_airtime_us(size_t bytes)
{
    /* HT20 MCS0: 36 us of preambles, then 26 data bits per 4 us symbol, plus 22 service and tail bits */
    int64_t bits = 8 * ((int64_t)bytes + ESPNOW_OVERHEAD_BYTES) + 22;
    return 36 + 4 * ((bits + 25) / 26);
}

uint8_t rs_max_members(uint32_t round_hz, uint32_t guard_us)
{
    if (round_hz == 0) {
        return 0;
    }
    int64_t period = 1000000 / (int64_t)round_hz;
    for (unsigned n = RB_MAX_NODES; n >= 2; n--) {
        int64_t need = rs_airtime_us(RB_HEADER_BYTES + RB_REPORT_BYTES * (n - 1)) + (int64_t)guard_us;
        if (need < RS_MIN_SLOT_US) {
            need = RS_MIN_SLOT_US;
        }
        if (period / (n + 1) >= need) {
            return (uint8_t)n;
        }
    }
    return 0;
}

uint8_t rs_lateness_code(int64_t late_us)
{
    if (late_us < 0) {
        late_us = 0;
    }
    int64_t c = late_us / RS_LATENESS_UNIT_US + 1;
    return (uint8_t)(c > RS_LATENESS_MAX_CODE ? RS_LATENESS_MAX_CODE : c);
}

/* ---------------------------------------------------------------- the schedule */

static uint8_t slots(const rs_t *s)
{
    return (uint8_t)(members(s->membership) + 1);
}

static int64_t slot_us(const rs_t *s)
{
    return s->period_us / slots(s);
}

static uint16_t wire_round(const rs_t *s, int64_t k)
{
    return (uint16_t)(s->base_round + (uint16_t)k);
}

/* The round k (counted from base) that carries round number r. */
static int64_t k_of_round(const rs_t *s, uint16_t r)
{
    return (int16_t)(uint16_t)(r - s->base_round);
}

static bool missing(const rs_t *s, uint8_t index, int64_t now)
{
    return now - s->seen_us[index] > RS_MISSING_ROUNDS * s->period_us;
}

static void all_seen(rs_t *s, int64_t t)
{
    for (int i = 0; i < RB_MAX_NODES; i++) {
        s->seen_us[i] = t;
    }
}

static void begin_listening(rs_t *s, int64_t now)
{
    /* the round that starts now gets the next number: after anything sent, the one after it */
    s->base_us = now;
    s->base_round = s->sent_any ? (uint16_t)(s->last_sent_round + 1) : (uint16_t)rnd(s);
    s->sched_offset_known = false;
    s->state = RS_LISTENING;
    s->my_index = RS_NO_INDEX;
    s->membership = 0;
    s->id_known = 0;
    s->sched_known = false;
    s->synced = false;
    s->grant_pending = false;
    s->listen_until_us = now + LISTEN_MIN_ROUNDS * s->period_us
                         + (int64_t)(rnd(s) % (uint32_t)(LISTEN_SPREAD_ROUNDS * s->period_us));
}

/* Is round number r (this node's) one the scheduler numbers even (join requests) rather than odd (grants)? */
static bool sched_even(const rs_t *s, uint16_t r)
{
    bool scheduler = s->state == RS_MEMBER && rs_slot_of(s->membership, s->my_index) == 0;
    return !((uint16_t)(r + (uint16_t)(scheduler ? 0 : s->sched_offset)) & 1u);
}

/* After the phase was set outright at time t: keep counting from the number the round running at t had. */
static void keep_numbering(rs_t *s, uint16_t before, int64_t t)
{
    if (s->sent_any && (int16_t)(uint16_t)(s->last_sent_round + 1 - before) > 0) {
        before = (uint16_t)(s->last_sent_round + 1);
    }
    s->base_round = (uint16_t)(before - (uint16_t)floor_div(t - s->base_us, s->period_us));
}

static void found(rs_t *s, int64_t at)
{
    s->state = RS_MEMBER;
    s->version = (uint8_t)rnd(s);
    s->my_index = 0;
    s->membership = 1u;
    memcpy(s->member_id[0], s->id, RB_NODE_ID_BYTES);
    s->id_known = 1u;
    memcpy(s->sched_id, s->id, RB_NODE_ID_BYTES);
    s->sched_known = true;
    s->synced = true;
    s->base_round = wire_round(s, floor_div(at - s->base_us, s->period_us));   /* the number promised by rs_next */
    s->base_us = at;
    s->last_related_us = at;
    all_seen(s, at);
    s->restarts = ALL_INDICES;
    s->count.joins++;
    s->count.founded++;
}

/* Plan the first join request of a joiner: one of the next BACKOFF_START request rounds after t. */
static void plan_requests(rs_t *s, int64_t t)
{
    uint16_t r = wire_round(s, floor_div(t - s->base_us, s->period_us) + 1);
    if (!sched_even(s, r)) {
        r++;
    }
    s->backoff_window = BACKOFF_START;
    s->next_request_round = (uint16_t)(r + 2 * (rnd(s) % s->backoff_window));
}

/* Use a round broadcast of this node's schedule for timing. Returns how far into its slot it started on air. */
static int64_t timing(rs_t *s, int64_t t_rx, const rb_broadcast_t *b)
{
    int64_t latency = s->latency_known ? s->latency_us : 0;
    int64_t start = t_rx - rs_airtime_us(broadcast_bytes(b->membership)) - latency;   /* on air, by this clock */
    int64_t implied = start - (int64_t)b->slot * (s->period_us / b->slots_per_round);  /* its round's start */
    if (!s->synced) {
        s->base_us = implied;   /* the caller keeps the numbering (keep_numbering) */
        s->synced = true;
        s->last_k = 0;
        return latency;
    }
    int64_t k = floor_div(implied - s->base_us + s->period_us / 2, s->period_us);
    int64_t err = implied - (s->base_us + k * s->period_us);
    s->base_us += err / 2;
    s->last_k = k;
    return err + latency;
}

/* Sync to the schedule of round broadcast b and ask to join it. */
static void target(rs_t *s, int64_t t_rx, const rb_broadcast_t *b)
{
    s->state = RS_JOINING;
    s->version = b->schedule_version;
    s->membership = b->membership;
    s->my_index = RS_NO_INDEX;
    s->id_known = bit(b->node_index);
    memcpy(s->member_id[b->node_index], b->node_id, RB_NODE_ID_BYTES);
    s->sched_known = b->slot == 0;
    if (s->sched_known) {
        memcpy(s->sched_id, b->node_id, RB_NODE_ID_BYTES);
    }
    uint16_t before = wire_round(s, floor_div(t_rx - s->base_us, s->period_us));
    s->synced = false;
    s->grant_pending = false;
    timing(s, t_rx, b);
    keep_numbering(s, before, t_rx);
    s->sched_offset_known = b->slot == 0;
    s->sched_offset = (int16_t)(uint16_t)(b->round - wire_round(s, 0));
    s->last_related_us = t_rx;
    all_seen(s, t_rx);
    memset(s->lateness, 0, sizeof s->lateness);
    s->restarts = ALL_INDICES;
    plan_requests(s, t_rx);
}

static void become_member(rs_t *s, int64_t t, const rb_broadcast_t *grant)
{
    s->state = RS_MEMBER;
    s->version = grant->schedule_version;
    s->membership = grant->membership;
    s->my_index = grant->node_index;
    memcpy(s->member_id[s->my_index], s->id, RB_NODE_ID_BYTES);
    s->id_known = (s->id_known & s->membership) | bit(s->my_index);
    s->last_related_us = t;
    all_seen(s, t);
    s->restarts = ALL_INDICES;
    s->count.joins++;
}

/* Take a newer version of this node's schedule from one of its round broadcasts. */
static void adopt(rs_t *s, int64_t t, const rb_broadcast_t *b)
{
    uint32_t added = b->membership & ~s->membership, removed = s->membership & ~b->membership;
    s->restarts |= added;
    s->id_known &= ~(added | removed);
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if (added & bit(i)) {
            s->seen_us[i] = t;
        }
    }
    s->version = b->schedule_version;
    s->membership = b->membership;
    if (s->state == RS_MEMBER && !(s->membership & bit(s->my_index))) {
        s->count.evicted++;   /* dropped while still here (it was not heard): ask to join again */
        s->state = RS_JOINING;
        s->my_index = RS_NO_INDEX;
        plan_requests(s, t);
    }
}

/* Drop every member nobody has heard for RS_MISSING_ROUNDS rounds (only the slot 0 member does this). */
static void drop_missing(rs_t *s, int64_t now)
{
    uint32_t gone = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if ((s->membership & bit(i)) && i != s->my_index && missing(s, i, now)) {
            gone |= bit(i);
        }
    }
    if (gone) {
        s->membership &= ~gone;
        s->id_known &= ~gone;
        s->version++;
        s->count.dropped += members(gone);
    }
}

static void maintain(rs_t *s, int64_t now)
{
    if (s->state == RS_LISTENING) {
        if (now >= s->listen_until_us) {
            found(s, s->listen_until_us);
        }
        return;
    }
    if (s->state == RS_JOINING) {
        if (now - s->last_related_us > (RS_MISSING_ROUNDS + 1) * s->period_us) {
            begin_listening(s, now);   /* the schedule it was joining went quiet */
        }
        return;
    }
    if (s->grant_pending && now > s->grant_by_us) {
        s->grant_pending = false;
    }
    uint8_t my_slot = rs_slot_of(s->membership, s->my_index);
    if (my_slot > 0) {
        bool all_before_missing = true;
        for (uint8_t i = 0; i < s->my_index; i++) {
            if ((s->membership & bit(i)) && !missing(s, i, now)) {
                all_before_missing = false;
            }
        }
        if (all_before_missing) {   /* failover: the scheduler and everyone before this node are gone */
            drop_missing(s, now);
            memcpy(s->sched_id, s->id, RB_NODE_ID_BYTES);
            s->sched_known = true;
        }
    }
    if (s->membership == bit(s->my_index) && now - s->last_related_us > LONE_RESHUFFLE_ROUNDS * s->period_us) {
        /* alone and hearing nothing: maybe another lone founder sends in step with this one; move the phase */
        s->base_us += (int64_t)(rnd(s) % (uint32_t)s->period_us);
        s->last_related_us = now;
    }
}

/* ---------------------------------------------------------------- calls */

bool rs_init(rs_t *s, const uint8_t id[RB_NODE_ID_BYTES], uint32_t round_hz, uint32_t guard_us, uint32_t seed,
             int64_t now_us)
{
    memset(s, 0, sizeof *s);
    s->max_members = rs_max_members(round_hz, guard_us);
    if (s->max_members < 2) {
        return false;
    }
    memcpy(s->id, id, RB_NODE_ID_BYTES);
    s->period_us = 1000000 / (int64_t)round_hz;
    s->guard_us = guard_us;
    s->rng = seed ^ ((uint32_t)id[2] << 24 | (uint32_t)id[3] << 16 | (uint32_t)id[4] << 8 | id[5]);
    if (!s->rng) {
        s->rng = 0x9E3779B9u;
    }
    begin_listening(s, now_us);
    return true;
}

static bool related(const rs_t *s, const rb_broadcast_t *b)
{
    if (b->schedule_version == s->version && b->membership == s->membership) {
        return true;
    }
    if ((s->id_known & bit(b->node_index)) && same_id(s->member_id[b->node_index], b->node_id)) {
        return true;
    }
    return b->slot == 0 && s->sched_known && same_id(s->sched_id, b->node_id);
}

static void on_request(rs_t *s, int64_t t, const rb_broadcast_t *b)
{
    uint8_t index = RS_NO_INDEX;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if ((s->membership & s->id_known & bit(i)) && same_id(s->member_id[i], b->node_id)) {
            index = i;   /* a member that restarted: give it its index back */
        }
    }
    if (index == RS_NO_INDEX) {
        if (members(s->membership) >= s->max_members) {
            s->count.refused++;
            return;
        }
        index = (b->node_index < RB_MAX_NODES && !(s->membership & bit(b->node_index))) ? b->node_index : 0;
        while (s->membership & bit(index)) {
            index++;
        }
    }
    s->grant_pending = true;
    s->grant_index = index;
    memcpy(s->grant_id, b->node_id, RB_NODE_ID_BYTES);
    s->grant_by_us = t + GRANT_KEEP_ROUNDS * s->period_us;
    s->grant_round = b->round;
    s->grant_k = floor_div(t - s->base_us, s->period_us);
}

rs_heard_t rs_heard(rs_t *s, int64_t t_rx_us, const rb_broadcast_t *b)
{
    bool mine = same_id(b->node_id, s->id);
    if (b->slots_per_round == 0 || b->slot >= b->slots_per_round || b->node_index >= RB_MAX_NODES
            || (mine && b->frame_type != RB_FRAME_JOIN_GRANT)) {
        return RS_HEARD_IGNORED;
    }
    if (s->state == RS_LISTENING) {
        if (b->frame_type != RB_FRAME_ROUND) {
            return RS_HEARD_IGNORED;
        }
        target(s, t_rx_us, b);
        return RS_HEARD_OTHER;
    }
    uint32_t who = bit(b->node_index);
    if (b->frame_type == RB_FRAME_JOIN_GRANT) {
        if (mine) {
            if (s->state != RS_JOINING) {
                return RS_HEARD_IGNORED;
            }
            become_member(s, t_rx_us, b);
            return RS_HEARD_OTHER;
        }
        if (b->schedule_version == (uint8_t)(s->version + 1) && !(s->membership & who)
                && b->membership == (s->membership | who)) {
            s->version = b->schedule_version;   /* the scheduler admitted someone to this node's schedule */
            s->membership = b->membership;
            memcpy(s->member_id[b->node_index], b->node_id, RB_NODE_ID_BYTES);
            s->id_known |= who;
            s->seen_us[b->node_index] = t_rx_us;
            s->restarts |= who;
            s->last_related_us = t_rx_us;
            return RS_HEARD_OTHER;
        }
        return RS_HEARD_IGNORED;
    }
    if (b->frame_type == RB_FRAME_JOIN_REQUEST) {
        if (s->state == RS_MEMBER && rs_slot_of(s->membership, s->my_index) == 0
                && b->schedule_version == s->version && !s->grant_pending) {
            on_request(s, t_rx_us, b);
            return RS_HEARD_OTHER;
        }
        return RS_HEARD_IGNORED;
    }
    if (b->frame_type != RB_FRAME_ROUND) {
        return RS_HEARD_IGNORED;
    }
    if (!related(s, b)) {
        s->count.foreign++;
        if (b->slot == 0 && s->sched_known && memcmp(b->node_id, s->sched_id, RB_NODE_ID_BYTES) < 0) {
            if (s->state == RS_MEMBER) {
                s->count.merges++;   /* a schedule with a lower scheduler ID: leave this one and join it */
            }
            target(s, t_rx_us, b);
            return RS_HEARD_OTHER;
        }
        return RS_HEARD_IGNORED;
    }
    s->last_related_us = t_rx_us;
    int64_t late = timing(s, t_rx_us, b);
    if (b->slot == 0) {
        memcpy(s->sched_id, b->node_id, RB_NODE_ID_BYTES);
        s->sched_known = true;
        s->sched_offset = (int16_t)(uint16_t)(b->round - wire_round(s, s->last_k));
        s->sched_offset_known = true;
    }
    if (newer(b->schedule_version, s->version)) {
        adopt(s, t_rx_us, b);
    }
    if (!(s->membership & who)) {
        return RS_HEARD_OTHER;
    }
    if (!(s->id_known & who) || !same_id(s->member_id[b->node_index], b->node_id)) {
        if (s->id_known & who) {
            s->restarts |= who;   /* the index now names another node */
        }
        memcpy(s->member_id[b->node_index], b->node_id, RB_NODE_ID_BYTES);
        s->id_known |= who;
    }
    s->seen_us[b->node_index] = t_rx_us;
    if (b->schedule_version == s->version) {
        for (uint8_t k = 0; k < b->n_reports; k++) {   /* someone else heard a member: it is alive */
            const rb_report_t *r = &b->reports[k];
            if ((s->membership & bit(r->transmitter_index)) && r->heard && r->since < RB_COUNT_MAX) {
                int64_t ev = t_rx_us - (int64_t)(r->since + 1) * s->period_us;
                if (ev > s->seen_us[r->transmitter_index]) {
                    s->seen_us[r->transmitter_index] = ev;
                }
            }
        }
    }
    if (s->state != RS_MEMBER) {
        return RS_HEARD_OTHER;
    }
    s->lateness[b->node_index] = rs_lateness_code(late);
    return RS_HEARD_MEMBER;
}

/* When this node would send `kind` in round k, or -1 when it would not. */
static int64_t slot_time(const rs_t *s, rs_tx_kind_t kind, int64_t k)
{
    int64_t round_start = s->base_us + k * s->period_us;
    uint16_t r = wire_round(s, k);
    switch (kind) {
    case RS_TX_ROUND:
        if (s->state != RS_MEMBER) {
            return -1;
        }
        if (s->sent_any && (int16_t)(uint16_t)(r - s->last_sent_round) <= 0) {
            return -1;   /* round numbers never repeat or go back */
        }
        return round_start + rs_slot_of(s->membership, s->my_index) * slot_us(s);
    case RS_TX_JOIN_GRANT:
        if (s->state != RS_MEMBER || !s->grant_pending || rs_slot_of(s->membership, s->my_index) != 0
                || sched_even(s, r)) {
            return -1;
        }
        return round_start + (slots(s) - 1) * slot_us(s);
    case RS_TX_JOIN_REQUEST:
        if (s->state != RS_JOINING || !s->sched_offset_known || !sched_even(s, r)
                || (int16_t)(uint16_t)(r - s->next_request_round) < 0
                || (s->sent_any && (int16_t)(uint16_t)(r - s->last_sent_round) <= 0)) {
            return -1;
        }
        return round_start + (slots(s) - 1) * slot_us(s);
    case RS_TX_NONE:
        break;
    }
    return -1;
}

/* The first `kind` transmission strictly after `after`, looking a few rounds ahead. */
static rs_tx_t first(const rs_t *s, rs_tx_kind_t kind, int64_t after)
{
    rs_tx_t tx = {RS_TX_NONE, 0, 0};
    int64_t k = floor_div(after - s->base_us, s->period_us);   /* the round that is running at `after` */
    if (kind == RS_TX_JOIN_REQUEST) {
        int16_t wait = (int16_t)(uint16_t)(s->next_request_round - wire_round(s, k));
        if (wait > 0) {
            k += wait;
        }
    }
    if ((kind == RS_TX_ROUND || kind == RS_TX_JOIN_REQUEST) && s->sent_any) {
        int16_t behind = (int16_t)(uint16_t)(s->last_sent_round - wire_round(s, k));
        if (behind >= 0) {
            k += behind;   /* the round after the last one sent is the earliest */
        }
    }
    for (int n = 0; n < 4; n++, k++) {
        int64_t at = slot_time(s, kind, k);
        if (at > after) {
            tx.kind = kind;
            tx.at_us = at;
            tx.round = wire_round(s, k);
            return tx;
        }
    }
    return tx;
}

rs_tx_t rs_next(rs_t *s, int64_t after_us)
{
    maintain(s, after_us);
    if (s->state == RS_LISTENING) {
        rs_tx_t tx = {RS_TX_ROUND, s->listen_until_us,
                      wire_round(s, floor_div(s->listen_until_us - s->base_us, s->period_us))};
        return tx;
    }
    rs_tx_t best = {RS_TX_NONE, 0, 0};
    const rs_tx_kind_t kinds[3] = {RS_TX_ROUND, RS_TX_JOIN_GRANT, RS_TX_JOIN_REQUEST};
    for (int i = 0; i < 3; i++) {
        rs_tx_t tx = first(s, kinds[i], after_us);
        if (tx.kind != RS_TX_NONE && (best.kind == RS_TX_NONE || tx.at_us < best.at_us)) {
            best = tx;
        }
    }
    if (best.kind == RS_TX_NONE) {   /* nothing within reach: wake up next round to look again */
        best.kind = RS_TX_JOIN_REQUEST;
        best.at_us = s->base_us + (floor_div(after_us - s->base_us, s->period_us) + 1) * s->period_us;
        best.round = s->next_request_round;
    }
    return best;
}

static void header(const rs_t *s, rb_broadcast_t *out, uint8_t frame, uint16_t round)
{
    memset(out, 0, sizeof *out);
    out->version = RB_VERSION;
    out->frame_type = frame;
    out->round = round;
    out->slots_per_round = slots(s);
    out->schedule_version = s->version;
    out->membership = s->membership;
}

bool rs_take(rs_t *s, const rs_tx_t *tx, int64_t now_us, rb_broadcast_t *out)
{
    maintain(s, now_us);
    int64_t at = slot_time(s, tx->kind, k_of_round(s, tx->round));
    if (at < 0 || now_us < at - s->guard_us) {
        return false;   /* the plan changed */
    }
    if (now_us > at + s->guard_us) {
        s->count.late_slots++;
        return false;
    }
    switch (tx->kind) {
    case RS_TX_ROUND:
        if (rs_slot_of(s->membership, s->my_index) == 0) {
            drop_missing(s, now_us);   /* the scheduler drops missing members as its round starts */
        }
        header(s, out, RB_FRAME_ROUND, tx->round);
        out->slot = rs_slot_of(s->membership, s->my_index);
        out->node_index = s->my_index;
        memcpy(out->node_id, s->id, RB_NODE_ID_BYTES);
        s->sent_any = true;
        s->last_sent_round = tx->round;
        return true;
    case RS_TX_JOIN_GRANT: {
        uint8_t spr = slots(s);   /* the slots of the schedule it is sent under; it goes in their join slot */
        uint32_t who = bit(s->grant_index);
        if (!(s->membership & who)) {
            s->seen_us[s->grant_index] = now_us;
            s->restarts |= who;
        }
        s->membership |= who;
        memcpy(s->member_id[s->grant_index], s->grant_id, RB_NODE_ID_BYTES);
        s->id_known |= who;
        s->version++;
        s->grant_pending = false;
        s->count.admitted++;
        /* in the joiner's numbering: its request's round number plus the rounds since */
        header(s, out, RB_FRAME_JOIN_GRANT,
               (uint16_t)(s->grant_round + (uint16_t)(k_of_round(s, tx->round) - s->grant_k)));
        out->slot = (uint8_t)(spr - 1);
        out->slots_per_round = spr;
        out->node_index = s->grant_index;
        memcpy(out->node_id, s->grant_id, RB_NODE_ID_BYTES);
        return true;
    }
    case RS_TX_JOIN_REQUEST: {
        header(s, out, RB_FRAME_JOIN_REQUEST, tx->round);
        out->slot = (uint8_t)(slots(s) - 1);
        uint8_t ask = 0;
        while (ask < RB_MAX_NODES - 1 && (s->membership & bit(ask))) {
            ask++;
        }
        out->node_index = ask;
        memcpy(out->node_id, s->id, RB_NODE_ID_BYTES);
        s->sent_any = true;
        s->last_sent_round = tx->round;
        s->count.requests++;
        s->next_request_round = (uint16_t)(tx->round + 2 + 2 * (rnd(s) % s->backoff_window));
        if (s->backoff_window < BACKOFF_MAX) {
            s->backoff_window = (uint8_t)(s->backoff_window * 2);
        }
        return true;
    }
    case RS_TX_NONE:
        break;
    }
    return false;
}

void rs_sent(rs_t *s, int64_t t_slot_us, int64_t t_done_us, size_t bytes)
{
    int64_t l = t_done_us - rs_airtime_us(bytes) - t_slot_us;
    if (l < 0) {
        l = 0;
    }
    if (l > s->guard_us) {
        l = s->guard_us;
    }
    if (!s->latency_known) {
        s->latency_us = l;
        s->latency_known = true;
    } else {
        s->latency_us += (l - s->latency_us) / 8;
    }
}

uint8_t rs_take_lateness(rs_t *s, uint8_t index)
{
    if (index >= RB_MAX_NODES) {
        return RS_LATENESS_NONE;
    }
    uint8_t c = s->lateness[index];
    s->lateness[index] = RS_LATENESS_NONE;
    return c;
}

uint32_t rs_take_restarts(rs_t *s)
{
    uint32_t r = s->restarts;
    s->restarts = 0;
    return r;
}
