/*
 * Motion score for one link, computed on the node: the C port of csi.detect.MotionScorer (GLOSSARY "Score").
 *
 * Per subcarrier, the amplitude goes through three cascaded high-pass stages (subtract an exponential running mean,
 * time constant 0.16 s of device time). The score is the variance of what is left over the last 1 s of device time
 * (the window), averaged across subcarriers. There is no score until the window holds at least 5 packets spanning
 * 0.9 s.
 *
 * Streaming statistics, not a stored window: the module keeps, per packet in the window, only the time and the sum of
 * squared residuals over subcarriers (exact), and per half-second bucket of device time the per-subcarrier sum of
 * residuals. The window mean is taken from those buckets, with the oldest bucket weighted by the share of its packets
 * still in the window, so it is the one approximate part. Against the shared vectors from the Python scorer
 * (tests/vectors/motion_score.txt) the score agrees within MOTION_SCORE_TOLERANCE_REL (see tests for the measured
 * spread), and the score code within one code in almost every packet.
 *
 * Portable C11: no allocation, no I/O, no platform calls. Builds on the PC (firmware/core/test.sh) and on the node.
 */
#ifndef MOTION_SCORE_H
#define MOTION_SCORE_H

#include <stdbool.h>
#include <stdint.h>

#ifndef MOTION_SCORE_MAX_SUBCARRIERS
#define MOTION_SCORE_MAX_SUBCARRIERS 64   /* the most subcarriers a link can have; sets the memory per link */
#endif
#ifndef MOTION_SCORE_RING
#define MOTION_SCORE_RING 48              /* most packets one window holds exactly (40 Hz needs 41) */
#endif

#define MOTION_SCORE_WINDOW_US 1000000    /* csi.detect.WINDOW_S */
#define MOTION_SCORE_MIN_PACKETS 5        /* csi.detect.MIN_WINDOW_PACKETS */
#define MOTION_SCORE_TAU_S 0.16f          /* csi.detect.HIGHPASS_TAU_S */
#define MOTION_SCORE_STAGES 3             /* csi.detect.HIGHPASS_STAGES */
#define MOTION_SCORE_BUCKETS_PER_WINDOW 2 /* window-mean resolution: 0.5 s buckets */
#define MOTION_SCORE_BUCKET_SLOTS (MOTION_SCORE_BUCKETS_PER_WINDOW + 1)

/* Stated tolerance against the Python score: |c - p| <= MOTION_SCORE_TOLERANCE_REL * p + MOTION_SCORE_TOLERANCE_ABS. */
#define MOTION_SCORE_TOLERANCE_REL 0.15f
#define MOTION_SCORE_TOLERANCE_ABS 1e-4f

/* Score code: the link report's u8 score. 0 = no score; code c >= 1 stands for 2^((c - 1) / 10 - 10).
 * Same formula as csi/score_code.py. */
#define MOTION_SCORE_CODE_NONE 0
#define MOTION_SCORE_CODE_STEPS_PER_OCTAVE 10
#define MOTION_SCORE_CODE_MIN_LOG2 (-10)

typedef enum {
    MOTION_SCORE_OK = 0,
    MOTION_SCORE_DROPPED,    /* not later than the last accepted packet: ignored and counted in `dropped` */
    MOTION_SCORE_RESTARTED,  /* the subcarrier count changed: the link started over with this packet */
    MOTION_SCORE_BAD_ARG,    /* NULL pointer, or n_sub outside 1..MOTION_SCORE_MAX_SUBCARRIERS */
} motion_score_status_t;

typedef struct {
    float sum[MOTION_SCORE_MAX_SUBCARRIERS];  /* per-subcarrier sum of residuals of the packets added */
    uint16_t added;                            /* packets added to this bucket */
    uint16_t expired;                          /* of those, packets that have left the window */
} motion_score_bucket_t;

/* One link's state. Allocate it statically or inside a larger struct; read only `dropped` and `overflowed`. */
typedef struct {
    float slow[MOTION_SCORE_STAGES][MOTION_SCORE_MAX_SUBCARRIERS];  /* high-pass running means */
    motion_score_bucket_t bucket[MOTION_SCORE_BUCKET_SLOTS];
    uint32_t ring_t[MOTION_SCORE_RING];      /* low 32 bits of each window packet's device time */
    float ring_sq[MOTION_SCORE_RING];        /* sum over subcarriers of its squared residuals */
    uint8_t ring_bucket[MOTION_SCORE_RING];  /* its bucket slot */
    int64_t last_t_us;                       /* device time of the last accepted packet */
    int64_t bucket_id;                       /* device time / bucket width of the newest bucket */
    uint16_t n_sub;                          /* 0 until the first packet */
    uint8_t ring_head;                       /* index of the oldest window packet */
    uint8_t ring_count;
    uint8_t bucket_slot;                     /* slot of the newest bucket */
    uint32_t dropped;                        /* packets ignored for not being later than the last accepted one */
    uint32_t overflowed;                     /* packets pushed out of a full window before they were 1 s old */
} motion_score_link_t;

/* Start (or restart) a link with no history. Use it to re-baseline a link, e.g. after a gain change. */
void motion_score_init(motion_score_link_t *s);

/* Feed one packet: the receiving node's device time in us (monotonic) and the amplitude of each kept subcarrier. */
motion_score_status_t motion_score_push(motion_score_link_t *s, int64_t t_us, const float *amp, uint16_t n_sub);

/* The score after the last accepted packet. Returns false while there is none (window not full). It does the window
 * arithmetic, so call it when building a link report, not on every packet. */
bool motion_score_get(const motion_score_link_t *s, float *score);

/* Score code for a score (>= 0; anything at or below the floor gives 1, above the top gives 255). */
uint8_t motion_score_encode(float score);

/* The score a code stands for. Returns false for MOTION_SCORE_CODE_NONE. */
bool motion_score_decode(uint8_t code, float *score);

#endif /* MOTION_SCORE_H */
