#include "raw_mode.h"

#include <string.h>

void raw_mode_init(raw_mode_t *r)
{
    memset(r, 0, sizeof *r);
}

raw_apply_t raw_mode_apply(raw_mode_t *r, const rb_request_t *q, const uint8_t my_id[RB_NODE_ID_BYTES],
                           uint16_t my_round)
{
    if (memcmp(q->target, my_id, RB_NODE_ID_BYTES) != 0) {
        r->count.ignored++;
        return RAW_NOT_MINE;
    }
    uint16_t age = (uint16_t)(my_round - q->target_round);   /* a target round ahead of this node's wraps to huge */
    if (age > RB_RAW_REQUEST_MAX_AGE) {
        r->count.ignored++;
        return RAW_STALE;
    }
    r->n = q->n < RB_RAW_MAX_LINKS ? q->n : RB_RAW_MAX_LINKS;
    for (uint8_t k = 0; k < r->n; k++) {
        memcpy(r->ids[k], q->transmitters[k], RB_NODE_ID_BYTES);
    }
    memset(r->kept, 0, sizeof r->kept);
    r->count.applied++;
    return RAW_APPLIED;
}

void raw_mode_heard(raw_mode_t *r, const uint8_t id[RB_NODE_ID_BYTES], uint8_t index, uint16_t round, int8_t rssi,
                    const int8_t *csi, size_t csi_len)
{
    size_t n = (csi_len < RB_RAW_IQ_MAX ? csi_len : RB_RAW_IQ_MAX) & ~(size_t)1;   /* whole I/Q pairs */
    for (uint8_t k = 0; k < r->n && csi && n >= 2; k++) {
        if (memcmp(r->ids[k], id, RB_NODE_ID_BYTES) == 0) {
            raw_capture_t *c = &r->kept[k];
            c->valid = true;
            c->index = index;
            c->round = round;
            c->rssi = rssi;
            c->n_iq = (uint8_t)n;
            memcpy(c->iq, csi, n);
        }
    }
}

uint8_t raw_mode_take(raw_mode_t *r, uint32_t membership, uint8_t my_index, size_t room,
                      rb_raw_t out[RB_RAW_MAX_LINKS])
{
    uint8_t n = 0;
    for (uint8_t k = 0; k < r->n; k++) {
        raw_capture_t *c = &r->kept[k];
        if (!c->valid) {
            continue;
        }
        c->valid = false;
        size_t need = RB_RAW_HEAD_BYTES + (size_t)c->n_iq;
        bool member = c->index < RB_MAX_NODES && c->index != my_index && (membership >> c->index & 1u);
        if (!member || need > room) {
            r->count.skipped++;
            continue;
        }
        out[n++] = (rb_raw_t){c->index, c->round, c->rssi, c->n_iq, c->iq};
        room -= need;
        r->count.sent++;
    }
    return n;
}

uint32_t raw_mode_flags(const raw_mode_t *r, const uint8_t member_id[RB_MAX_NODES][RB_NODE_ID_BYTES],
                        uint32_t id_known)
{
    uint32_t flags = 0;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        for (uint8_t k = 0; k < r->n && (id_known >> i & 1u); k++) {
            if (memcmp(member_id[i], r->ids[k], RB_NODE_ID_BYTES) == 0) {
                flags |= 1u << i;
            }
        }
    }
    return flags;
}
