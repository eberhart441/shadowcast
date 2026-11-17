#include "csi_layout.h"

#include <math.h>
#include <string.h>

void csi_layout_init(csi_layout_t *layout)
{
    memset(layout, 0, sizeof *layout);
}

static void start_warm_up(csi_layout_t *layout, uint16_t len)
{
    layout->raw_len = 0;
    layout->candidate_len = len;
    layout->warm = 0;
    layout->n_kept = 0;
    layout->other_run = 0;
    memset(layout->nonzero, 0, sizeof layout->nonzero);
}

static int emit(const csi_layout_t *layout, const int8_t *buf, float gain, float *amp)
{
    int n = 0;
    for (int i = 0; i < layout->raw_len / 2; i++) {
        if (layout->nonzero[i]) {
            amp[n++] = gain * hypotf((float)buf[2 * i], (float)buf[2 * i + 1]);
        }
    }
    return n;
}

/* A warm-up frame. Returns true when it completed the warm-up and the layout is now locked. */
static bool warm_up(csi_layout_t *layout, const int8_t *buf, uint16_t len)
{
    if (layout->candidate_len != len) {
        if (layout->warm) {
            return false;                  /* another length while warming up: skipped, as csi/parser.py does */
        }
        start_warm_up(layout, len);
    }
    for (int i = 0; i < len / 2; i++) {
        if (buf[2 * i] || buf[2 * i + 1]) {
            layout->nonzero[i] = true;
        }
    }
    if (++layout->warm < CSI_LAYOUT_WARMUP) {
        return false;
    }
    uint16_t kept = 0;
    for (int i = 0; i < len / 2; i++) {
        kept = (uint16_t)(kept + layout->nonzero[i]);
    }
    if (kept == 0 || kept > CSI_LAYOUT_MAX_KEPT) {
        if (kept) {
            layout->too_wide++;
        }
        start_warm_up(layout, len);        /* nothing usable: start over */
        return false;
    }
    layout->raw_len = len;
    layout->n_kept = kept;
    return true;
}

int csi_layout_amplitudes(csi_layout_t *layout, const int8_t *buf, uint16_t len, float gain, float *amp)
{
    if (len == 0 || len % 2 || len > CSI_LAYOUT_MAX_RAW) {
        layout->skipped++;
        return 0;
    }
    if (layout->raw_len == 0) {
        if (warm_up(layout, buf, len)) {
            return emit(layout, buf, gain, amp);
        }
        layout->skipped++;
        return 0;
    }
    if (len == layout->raw_len) {
        layout->other_run = 0;
        return emit(layout, buf, gain, amp);
    }
    layout->skipped++;
    if (layout->other_run && layout->other_len == len) {
        layout->other_run++;
    } else {
        layout->other_len = len;
        layout->other_run = 1;
    }
    if (layout->other_run >= CSI_LAYOUT_RELOCK) {
        layout->relocks++;
        start_warm_up(layout, len);
        layout->candidate_len = 0;         /* the next frame of this length starts the warm-up afresh */
    }
    return 0;
}
