#include "motion_score.h"

#include <math.h>
#include <string.h>

#define BUCKET_US ((int64_t)MOTION_SCORE_WINDOW_US / MOTION_SCORE_BUCKETS_PER_WINDOW)
#define FULL_SPAN_US (MOTION_SCORE_WINDOW_US / 10 * 9) /* a window counts once it spans 0.9 s */

_Static_assert(MOTION_SCORE_RING <= 255, "ring indices are uint8_t");
_Static_assert(MOTION_SCORE_MAX_SUBCARRIERS <= 65535, "n_sub is uint16_t");
_Static_assert(MOTION_SCORE_WINDOW_US % MOTION_SCORE_BUCKETS_PER_WINDOW == 0, "buckets must tile the window");

void motion_score_init(motion_score_link_t *s) {
    if (s) memset(s, 0, sizeof *s);
}

static int64_t bucket_of(int64_t t_us) {
    int64_t q = t_us / BUCKET_US;
    return (t_us % BUCKET_US < 0) ? q - 1 : q; /* floor, also for negative times */
}

static void expire_oldest(motion_score_link_t *s) {
    s->bucket[s->ring_bucket[s->ring_head]].expired++;
    s->ring_head = (uint8_t)((s->ring_head + 1) % MOTION_SCORE_RING);
    s->ring_count--;
}

/* Adds one packet's residuals to the window: its time and squared sum to the ring, its residuals to its bucket. */
static void add_to_window(motion_score_link_t *s, int64_t t_us, const float *residual) {
    int64_t id = bucket_of(t_us);
    if (s->ring_count == 0 || id != s->bucket_id) {
        /* Bucket ids only grow, so the slot reused here last held a bucket at least BUCKET_SLOTS ids older. All of its
         * packets are more than one window older than t_us, so expire_window has already let them go. */
        s->bucket_slot = (uint8_t)((s->bucket_slot + 1) % MOTION_SCORE_BUCKET_SLOTS);
        memset(&s->bucket[s->bucket_slot], 0, sizeof s->bucket[0]);
        s->bucket_id = id;
    }
    if (s->ring_count == MOTION_SCORE_RING) {
        expire_oldest(s); /* faster than the ring allows: the window gets shorter than 1 s */
        s->overflowed++;
    }
    motion_score_bucket_t *b = &s->bucket[s->bucket_slot];
    float sq = 0.0f;
    for (unsigned k = 0; k < s->n_sub; k++) {
        b->sum[k] += residual[k];
        sq += residual[k] * residual[k];
    }
    b->added++;
    unsigned tail = (s->ring_head + s->ring_count) % MOTION_SCORE_RING;
    s->ring_t[tail] = (uint32_t)t_us;
    s->ring_sq[tail] = sq;
    s->ring_bucket[tail] = s->bucket_slot;
    s->ring_count++;
}

/* Lets go of every packet more than one window older than t_us (csi.detect: t - t_oldest > WINDOW). */
static void expire_window(motion_score_link_t *s, int64_t t_us) {
    if (t_us - s->last_t_us > MOTION_SCORE_WINDOW_US) {
        while (s->ring_count) expire_oldest(s); /* everything is older; also keeps 32-bit ages below from wrapping */
        return;
    }
    uint32_t now = (uint32_t)t_us;
    while (s->ring_count && (uint32_t)(now - s->ring_t[s->ring_head]) > MOTION_SCORE_WINDOW_US) expire_oldest(s);
}

motion_score_status_t motion_score_push(motion_score_link_t *s, int64_t t_us, const float *amp, uint16_t n_sub) {
    if (!s || !amp || n_sub == 0 || n_sub > MOTION_SCORE_MAX_SUBCARRIERS) return MOTION_SCORE_BAD_ARG;
    motion_score_status_t status = MOTION_SCORE_OK;
    if (s->n_sub != 0 && n_sub != s->n_sub) {
        uint32_t dropped = s->dropped, overflowed = s->overflowed;
        motion_score_init(s);
        s->dropped = dropped;
        s->overflowed = overflowed;
        status = MOTION_SCORE_RESTARTED;
    }
    float residual[MOTION_SCORE_MAX_SUBCARRIERS];
    if (s->n_sub == 0) {
        /* The first mean starts at the first sample and the later ones at zero, so there is no transient; the first
         * packet's residual is zero and it still joins the window (as in csi.detect). */
        s->n_sub = n_sub;
        for (unsigned k = 0; k < n_sub; k++) {
            s->slow[0][k] = amp[k];
            residual[k] = 0.0f;
        }
        s->last_t_us = t_us;
        add_to_window(s, t_us, residual);
        return status;
    }
    if (t_us <= s->last_t_us) {
        s->dropped++;
        return MOTION_SCORE_DROPPED;
    }
    expire_window(s, t_us);
    float alpha = (float)(1.0 - exp(-(double)(t_us - s->last_t_us) / 1e6 / (double)MOTION_SCORE_TAU_S));
    s->last_t_us = t_us;
    for (unsigned k = 0; k < n_sub; k++) {
        float x = amp[k];
        for (unsigned st = 0; st < MOTION_SCORE_STAGES; st++) {
            s->slow[st][k] += alpha * (x - s->slow[st][k]);
            x -= s->slow[st][k];
        }
        residual[k] = x;
    }
    add_to_window(s, t_us, residual);
    return status;
}

bool motion_score_get(const motion_score_link_t *s, float *score) {
    if (!s || !score || s->n_sub == 0 || s->ring_count < MOTION_SCORE_MIN_PACKETS) return false;
    /* A full ring also counts as a full window: above ~45 packets/s per link the window is shorter than 1 s, but the
     * link still scores (and `overflowed` says so) instead of going quiet. */
    bool spans = (uint32_t)((uint32_t)s->last_t_us - s->ring_t[s->ring_head]) >= FULL_SPAN_US;
    if (!spans && s->ring_count < MOTION_SCORE_RING) return false;
    unsigned n = s->ring_count, k_n = s->n_sub;
    float sq = 0.0f;
    for (unsigned i = 0; i < n; i++) sq += s->ring_sq[(s->ring_head + i) % MOTION_SCORE_RING];
    /* Window mean per subcarrier from the buckets. A bucket some of whose packets have left the window counts with
     * the share still in it; that is the one approximation. */
    float sum[MOTION_SCORE_MAX_SUBCARRIERS] = {0};
    for (unsigned b = 0; b < MOTION_SCORE_BUCKET_SLOTS; b++) {
        const motion_score_bucket_t *bk = &s->bucket[b];
        if (bk->added == 0 || bk->expired >= bk->added) continue;
        float w = (float)(bk->added - bk->expired) / (float)bk->added;
        for (unsigned k = 0; k < k_n; k++) sum[k] += w * bk->sum[k];
    }
    float mean_sq = 0.0f;
    for (unsigned k = 0; k < k_n; k++) {
        float m = sum[k] / (float)n;
        mean_sq += m * m;
    }
    float v = (sq / (float)n - mean_sq) / (float)k_n;
    *score = v > 0.0f ? v : 0.0f;
    return true;
}

uint8_t motion_score_encode(float score) {
    if (isnan(score)) return MOTION_SCORE_CODE_NONE;
    if (score <= 0.0f) return 1;
    float steps = (float)MOTION_SCORE_CODE_STEPS_PER_OCTAVE * (log2f(score) - (float)MOTION_SCORE_CODE_MIN_LOG2) + 0.5f;
    if (steps >= 255.0f) return 255;
    if (steps < 0.0f) return 1;
    return (uint8_t)((unsigned)floorf(steps) + 1u);
}

bool motion_score_decode(uint8_t code, float *score) {
    if (code == MOTION_SCORE_CODE_NONE || !score) return false;
    *score = exp2f((float)(code - 1) / (float)MOTION_SCORE_CODE_STEPS_PER_OCTAVE + (float)MOTION_SCORE_CODE_MIN_LOG2);
    return true;
}
