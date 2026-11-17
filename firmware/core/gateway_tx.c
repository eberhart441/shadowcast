#include "gateway_tx.h"

#include <string.h>

#include "round_scheduler.h"

#define PERIOD_MIN_US 5000        /* round periods outside these are not ours (or a mismeasure) */
#define PERIOD_MAX_US 1000000
#define PERIOD_ROUNDS_MAX 16      /* two slot 0s at most this many rounds apart measure the period */

void gt_init(gt_t *g)
{
    memset(g, 0, sizeof *g);
    g->sent_start_us = INT64_MIN;
}

bool gt_queue(gt_t *g, const uint8_t *frame, size_t len, int64_t now_us)
{
    if (g->n == GT_QUEUE || len == 0 || len > GT_FRAME_MAX) {
        g->count.dropped++;
        return false;
    }
    gt_frame_t *f = &g->q[g->n++];
    f->len = (uint8_t)len;
    memcpy(f->bytes, frame, len);
    f->queued_us = now_us;
    return true;
}

void gt_heard(gt_t *g, int64_t t_rx_us, const rb_broadcast_t *b)
{
    if (!rb_carries_reports(b->frame_type) || b->slot != 0 || b->slots_per_round < 2) {
        return;
    }
    size_t bytes = b->len ? b->len : rb_size(b->membership);
    int64_t start = t_rx_us - rs_airtime_us(bytes);
    bool same = g->heard && memcmp(b->node_id, g->sched_id, RB_NODE_ID_BYTES) == 0;
    if (g->heard && !same) {
        bool lower = memcmp(b->node_id, g->sched_id, RB_NODE_ID_BYTES) < 0;
        if (!lower && t_rx_us - g->round_start_us <= GT_SCHEDULE_STALE_US) {
            return;   /* another schedule with a higher scheduler ID: it will merge into ours */
        }
        g->period_us = 0;   /* another scheduler: measure its period afresh */
    }
    if (same) {
        uint16_t rounds = (uint16_t)(b->round - g->round);
        if (rounds >= 1 && rounds <= PERIOD_ROUNDS_MAX) {
            int64_t p = (start - g->round_start_us) / rounds;
            if (p >= PERIOD_MIN_US && p <= PERIOD_MAX_US) {
                g->period_us = g->period_us ? (3 * g->period_us + p) / 4 : p;
            }
        }
    }
    g->heard = true;
    memcpy(g->sched_id, b->node_id, RB_NODE_ID_BYTES);
    g->round_start_us = start;
    g->round = b->round;
    g->slots = b->slots_per_round;
}

static void drop_stale(gt_t *g, int64_t now_us)
{
    uint8_t kept = 0;
    for (uint8_t k = 0; k < g->n; k++) {
        if (now_us - g->q[k].queued_us > GT_STALE_US) {
            g->count.stale++;
        } else {
            g->q[kept++] = g->q[k];
        }
    }
    g->n = kept;
}

/* The join slot's start of the last heard round, or -1 when no frame may go out in it. */
static int64_t join_slot(const gt_t *g)
{
    if (!g->heard || !g->period_us || (g->round & 1u) || g->sent_start_us == g->round_start_us) {
        return -1;
    }
    return g->round_start_us + (int64_t)(g->slots - 1) * g->period_us / g->slots;
}

int64_t gt_next(gt_t *g, int64_t now_us)
{
    drop_stale(g, now_us);
    int64_t at = join_slot(g);
    return g->n && at > now_us ? at : -1;
}

size_t gt_take(gt_t *g, int64_t now_us, uint8_t *out, size_t cap)
{
    drop_stale(g, now_us);
    int64_t at = join_slot(g);
    if (!g->n || at < 0 || now_us < at - GT_EARLY_US || now_us > at + GT_LATE_US || g->q[0].len > cap) {
        return 0;
    }
    size_t len = g->q[0].len;
    memcpy(out, g->q[0].bytes, len);
    memmove(&g->q[0], &g->q[1], (size_t)(g->n - 1) * sizeof g->q[0]);
    g->n--;
    g->sent_start_us = g->round_start_us;
    g->count.sent++;
    return len;
}

static int hex_digit(char c)
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

bool gt_parse_send(const char *line, uint8_t *out, size_t cap, size_t *len)
{
    if (strncmp(line, "send ", 5) != 0) {
        return false;
    }
    const char *p = line + 5;
    size_t n = 0;
    while (p[0] && p[0] != ' ') {
        int hi = hex_digit(p[0]), lo = p[1] ? hex_digit(p[1]) : -1;
        if (hi < 0 || lo < 0 || n == cap) {
            return false;
        }
        out[n++] = (uint8_t)(hi << 4 | lo);
        p += 2;
    }
    *len = n;
    return n > 0;
}
