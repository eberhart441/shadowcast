#include "link_table.h"

#include <string.h>

#define HISTORY_MASK 0x7FFFu   /* 15 rounds, RB_COUNT_MAX */

void link_table_init(link_table_t *table)
{
    memset(table, 0, sizeof *table);
    for (int i = 0; i < RB_MAX_NODES; i++) {
        motion_score_init(&table->link[i].score);
        table->link[i].rssi = RB_RSSI_NONE;
    }
}

void link_table_heard(link_table_t *table, uint8_t transmitter, int64_t t_us, int8_t rssi, const float *amp,
                      uint16_t n_sub)
{
    if (transmitter >= RB_MAX_NODES) {
        return;
    }
    link_state_t *l = &table->link[transmitter];
    l->history |= 1u;
    l->rssi = rssi;
    l->ever_heard = true;
    if (amp && n_sub) {
        motion_score_push(&l->score, t_us, amp, n_sub);
    }
}

void link_table_restart(link_table_t *table, uint8_t transmitter)
{
    if (transmitter < RB_MAX_NODES) {
        motion_score_init(&table->link[transmitter].score);
    }
}

static uint8_t popcount15(uint16_t h)
{
    uint8_t n = 0;
    for (h &= HISTORY_MASK; h; h &= (uint16_t)(h - 1)) {
        n++;
    }
    return n;
}

static uint8_t rounds_since(uint16_t h)
{
    h &= HISTORY_MASK;
    uint8_t k = 0;
    while (k < RB_COUNT_MAX && !(h >> k & 1u)) {
        k++;
    }
    return k;
}

uint8_t link_table_close_round(link_table_t *table, uint32_t membership, uint8_t me, rb_report_t *reports)
{
    uint8_t n = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        link_state_t *l = &table->link[i];
        if (membership >> i & 1u && i != me) {
            rb_report_t *r = &reports[n++];
            float score;
            r->transmitter_index = i;
            r->rssi = l->ever_heard ? l->rssi : RB_RSSI_NONE;
            r->heard = popcount15(l->history);
            r->since = rounds_since(l->history);
            r->lateness = RB_LATENESS_NONE_CODE;
            if (motion_score_get(&l->score, &score)) {
                r->score = motion_score_encode(score);
                r->flags = 0;
            } else {
                r->score = MOTION_SCORE_CODE_NONE;
                r->flags = RB_FLAG_WARMING;
            }
        }
        l->history = (uint16_t)((l->history << 1) & HISTORY_MASK);
    }
    return n;
}
