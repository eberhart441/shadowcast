#include "fixed_round.h"

#include <string.h>

#define MAX_MEMBERS 32

bool fixed_round_init(fixed_round_t *r, uint8_t index, uint8_t nodes, uint32_t round_hz, int64_t now_us)
{
    memset(r, 0, sizeof *r);
    if (nodes < 2 || nodes > MAX_MEMBERS || index >= nodes || round_hz == 0) {
        return false;
    }
    r->index = index;
    r->nodes = nodes;
    r->period_us = 1000000 / (int64_t)round_hz;
    r->slot_us = r->period_us / nodes;
    if (r->slot_us < FIXED_ROUND_MIN_SLOT_US) {
        return false;
    }
    r->base_us = now_us;
    return true;
}

/* floor(a / b) for b > 0 */
static int64_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    return (a % b != 0 && (a < 0)) ? q - 1 : q;
}

void fixed_round_heard(fixed_round_t *r, int64_t t_rx_us, uint16_t round, uint8_t slot, uint8_t slots_per_round)
{
    if (slots_per_round != r->nodes || slot >= r->nodes || slot == r->index) {
        r->ignored++;
        return;
    }
    int64_t implied = t_rx_us - (int64_t)slot * r->slot_us;   /* when that round started, by this node's clock */
    if (r->synced) {
        /* Phase error to the nearest round start of ours, in [-P/2, P/2); move halfway. */
        int64_t e = implied - r->base_us;
        e -= floor_div(e + r->period_us / 2, r->period_us) * r->period_us;
        r->base_us += e / 2;
        r->corrections++;
    } else {
        r->base_us = implied;
        r->synced = true;
    }
    /* Renumber: the round of ours that starts nearest `implied` is `round`. */
    int64_t j = floor_div(implied - r->base_us + r->period_us / 2, r->period_us);
    r->base_us += j * r->period_us;
    r->base_round = round;
}

int64_t fixed_round_next_tx(const fixed_round_t *r, int64_t after_us, uint16_t *round)
{
    int64_t first = r->base_us + (int64_t)r->index * r->slot_us;   /* this node's slot in round base_round */
    int64_t k = floor_div(after_us - first, r->period_us) + 1;   /* may be negative: round numbers wrap */
    *round = (uint16_t)(r->base_round + (uint16_t)k);
    return first + k * r->period_us;
}

uint32_t fixed_round_membership(const fixed_round_t *r)
{
    return r->nodes >= 32 ? 0xFFFFFFFFu : (1u << r->nodes) - 1u;
}
