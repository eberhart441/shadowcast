/*
 * CSI layout on the node: the raw CSI buffer of a frame (int8 pairs, imaginary then real, per subcarrier) as one
 * amplitude per kept subcarrier, with the null subcarriers dropped.
 *
 * Which subcarriers are null depends on the frame format (HT20 here; the hackathon rig ran HT40), so the layout is
 * learned as csi/parser.py learns an unknown raw length: the first CSI_LAYOUT_WARMUP frames of a length are a
 * warm-up and give no amplitudes; a subcarrier is kept if it is non-zero in any of them. Frames of another length
 * are skipped and counted; CSI_LAYOUT_RELOCK frames in a row of one other length re-lock onto it. A layout with more
 * than CSI_LAYOUT_MAX_KEPT kept subcarriers (more than a link can score) is skipped and counted, never truncated.
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef CSI_LAYOUT_H
#define CSI_LAYOUT_H

#include <stdbool.h>
#include <stdint.h>

#include "motion_score.h"

#define CSI_LAYOUT_MAX_RAW 256                         /* bytes: up to 128 subcarriers */
#define CSI_LAYOUT_MAX_KEPT MOTION_SCORE_MAX_SUBCARRIERS
#define CSI_LAYOUT_WARMUP 20                           /* csi.parser.WARMUP_PACKETS */
#define CSI_LAYOUT_RELOCK 50                           /* frames in a row of another length before re-locking */

typedef struct {
    uint16_t raw_len;                          /* the locked length in bytes; 0 while warming up */
    uint16_t candidate_len;                    /* the length being warmed up */
    uint8_t warm;                              /* warm-up frames seen of candidate_len */
    bool nonzero[CSI_LAYOUT_MAX_RAW / 2];      /* warm-up: subcarrier non-zero in any frame; locked: kept */
    uint16_t n_kept;
    uint16_t other_len;                        /* locked: the length of the current run of other-length frames */
    uint8_t other_run;
    uint32_t skipped;                          /* frames that gave no amplitudes (warm-up, other length, too wide) */
    uint32_t too_wide;                         /* warm-ups that ended with more kept subcarriers than a link scores */
    uint32_t relocks;
} csi_layout_t;

void csi_layout_init(csi_layout_t *layout);

/* Amplitudes of one frame's CSI, each times `gain` (the gain compensation), written to amp. Returns how many (the
 * kept subcarriers), or 0 when this frame gives none. amp must hold CSI_LAYOUT_MAX_RAW / 2 values. */
int csi_layout_amplitudes(csi_layout_t *layout, const int8_t *buf, uint16_t len, float gain, float *amp);

#endif /* CSI_LAYOUT_H */
