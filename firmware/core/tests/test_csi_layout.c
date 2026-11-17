/*
 * The node's CSI layout: one amplitude per kept subcarrier, nulls dropped the way csi/parser.py drops them for an
 * unknown raw length (the first 20 frames of a length are a warm-up; a subcarrier is kept if it is non-zero in any of
 * them), and a new layout locked when the frames keep arriving with another length.
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../csi_layout.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

/* A frame of n_sub subcarriers: pairs (imag, real) = (3, 4) (amplitude 5), except the listed nulls (0, 0). */
static uint16_t make_frame(int8_t *buf, int n_sub, const int *nulls, int n_nulls)
{
    for (int i = 0; i < n_sub; i++) {
        buf[2 * i] = 3;
        buf[2 * i + 1] = -4;
    }
    for (int k = 0; k < n_nulls; k++) {
        buf[2 * nulls[k]] = buf[2 * nulls[k] + 1] = 0;
    }
    return (uint16_t)(2 * n_sub);
}

int main(void)
{
    static csi_layout_t layout;
    int8_t buf[CSI_LAYOUT_MAX_RAW];
    float amp[CSI_LAYOUT_MAX_RAW / 2];
    const int nulls[] = {0, 27, 28, 29};

    csi_layout_init(&layout);
    uint16_t len = make_frame(buf, 57, nulls, 4);
    int n = 0;
    for (int i = 0; i < CSI_LAYOUT_WARMUP; i++) {
        n = csi_layout_amplitudes(&layout, buf, len, 1.0f, amp);
        CHECK(i == CSI_LAYOUT_WARMUP - 1 ? n == 0 || n == 53 : n == 0, "warm-up frame %d gave %d amplitudes", i, n);
    }
    n = csi_layout_amplitudes(&layout, buf, len, 1.0f, amp);
    CHECK(n == 53, "after the warm-up: %d kept subcarriers, expected 57 - 4 nulls", n);
    CHECK(n > 0 && fabsf(amp[0] - 5.0f) < 1e-5f && fabsf(amp[n - 1] - 5.0f) < 1e-5f, "amplitude is hypot(I, Q)");
    n = csi_layout_amplitudes(&layout, buf, len, 2.0f, amp);
    CHECK(n == 53 && fabsf(amp[0] - 10.0f) < 1e-5f, "the gain compensation scales the amplitude");

    /* A subcarrier that was non-zero in one warm-up frame is kept, even if it is zero now. */
    csi_layout_init(&layout);
    for (int i = 0; i < CSI_LAYOUT_WARMUP; i++) {
        len = make_frame(buf, 57, nulls, i == 5 ? 3 : 4);   /* frame 5 has subcarrier 29 non-zero */
        csi_layout_amplitudes(&layout, buf, len, 1.0f, amp);
    }
    len = make_frame(buf, 57, nulls, 4);
    CHECK(csi_layout_amplitudes(&layout, buf, len, 1.0f, amp) == 54, "kept if non-zero in any warm-up frame");

    /* Frames of another length are skipped and counted, until they are the steady layout: then it re-locks. */
    uint32_t skipped = layout.skipped;
    len = make_frame(buf, 64, nulls, 1);
    for (int i = 0; i < CSI_LAYOUT_RELOCK - 1; i++) {
        CHECK(csi_layout_amplitudes(&layout, buf, len, 1.0f, amp) == 0, "another length is skipped");
    }
    CHECK(layout.skipped == skipped + CSI_LAYOUT_RELOCK - 1, "skipped frames are counted");
    int got = 0;
    for (int i = 0; i < CSI_LAYOUT_RELOCK + CSI_LAYOUT_WARMUP && !got; i++) {
        got = csi_layout_amplitudes(&layout, buf, len, 1.0f, amp);
    }
    CHECK(got == 63, "re-locked onto the new length: %d kept", got);
    CHECK(layout.relocks == 1, "one re-lock counted");

    /* One stray frame of another length does not disturb a locked layout. */
    uint16_t other = make_frame(buf, 57, nulls, 4);
    csi_layout_amplitudes(&layout, buf, other, 1.0f, amp);
    len = make_frame(buf, 64, nulls, 1);
    CHECK(csi_layout_amplitudes(&layout, buf, len, 1.0f, amp) == 63, "a stray frame leaves the layout locked");

    /* More kept subcarriers than a link can score: skipped, never truncated. */
    csi_layout_init(&layout);
    len = make_frame(buf, CSI_LAYOUT_MAX_RAW / 2, nulls, 0);
    got = 0;
    for (int i = 0; i < 2 * CSI_LAYOUT_WARMUP; i++) {
        got |= csi_layout_amplitudes(&layout, buf, len, 1.0f, amp);
    }
    CHECK(got == 0 && layout.too_wide > 0, "a layout wider than %d kept subcarriers is skipped",
          CSI_LAYOUT_MAX_KEPT);
    CHECK(csi_layout_amplitudes(&layout, buf, 7, 1.0f, amp) == 0, "an odd length is not I/Q pairs");

    printf("csi_layout: %d failures\n", failures);
    return failures ? 1 : 0;
}
