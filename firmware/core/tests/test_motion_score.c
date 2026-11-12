/*
 * Tests for the node's motion score (motion_score.h) at its public interface.
 *
 *   test_motion_score <path to tests/vectors/motion_score.txt>
 *
 * The vectors come from the Python scorer (scripts/gen_score_vectors.py); the C score must match them within the
 * stated tolerance, and the score code exactly. Exit status 0 when every check passes.
 */
#define _CRT_SECURE_NO_WARNINGS /* the MSVC runtime (clang on Windows) deprecates fopen and strtok */
#include "../motion_score.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LINKS_PER_NODE 29             /* 30 nodes: every other node is a link */
#define MEMORY_BUDGET_BYTES (64 * 1024) /* for all links of one node */
#define MAX_ERRORS 1000000

static int failures;

#define CHECK(cond, ...)                                       \
    do {                                                       \
        if (!(cond)) {                                         \
            failures++;                                        \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
            fprintf(stderr, __VA_ARGS__);                      \
            fputc('\n', stderr);                               \
        }                                                      \
    } while (0)

static int cmp_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static double *errors;
static size_t n_errors;

/* Prints the spread of errors[from..n_errors), sorting that part. */
static void report_spread(const char *what, size_t from) {
    size_t n = n_errors - from;
    double *e = errors + from;
    if (n == 0) return;
    qsort(e, n, sizeof *e, cmp_double);
    printf("  %-16s %5zu scores, relative error median %.5f p99 %.5f max %.5f\n", what, n, e[n / 2],
           e[(size_t)(0.99 * (double)(n - 1))], e[n - 1]);
}

/* Replays every case of the vector file through a fresh link and compares each packet's result. */
static void test_vectors_match_python(const char *path) {
    FILE *f = fopen(path, "r");
    CHECK(f != NULL, "cannot open %s", path);
    if (!f) return;
    static char line[16384];
    static float amp[MOTION_SCORE_MAX_SUBCARRIERS];
    static motion_score_link_t link;
    char name[64] = "";
    unsigned n_sub = 0, packets = 0, cases = 0, enc = 0, dec = 0, code_off_by[4] = {0};
    long line_no = 0;
    size_t case_from = 0;
    errors = malloc(MAX_ERRORS * sizeof *errors);
    while (fgets(line, sizeof line, f)) {
        line_no++;
        char *kind = strtok(line, " \r\n");
        if (!kind || kind[0] == '#') continue;
        if (strcmp(kind, "case") == 0) {
            if (cases) report_spread(name, case_from);
            case_from = n_errors;
            snprintf(name, sizeof name, "%s", strtok(NULL, " \r\n"));
            n_sub = (unsigned)strtoul(strtok(NULL, " \r\n"), NULL, 10);
            motion_score_init(&link);
            cases++;
        } else if (strcmp(kind, "p") == 0 || strcmp(kind, "d") == 0) {
            int64_t t_us = strtoll(strtok(NULL, " \r\n"), NULL, 10);
            char *expected_text = strtok(NULL, " \r\n");
            for (unsigned k = 0; k < n_sub; k++) amp[k] = strtof(strtok(NULL, " \r\n"), NULL);
            motion_score_status_t st = motion_score_push(&link, t_us, amp, (uint16_t)n_sub);
            motion_score_status_t want = kind[0] == 'd' ? MOTION_SCORE_DROPPED : MOTION_SCORE_OK;
            CHECK(st == want, "%s line %ld: status %d, want %d", name, line_no, (int)st, (int)want);
            float got = 0;
            bool has = motion_score_get(&link, &got);
            bool want_has = strcmp(expected_text, "-") != 0;
            CHECK(has == want_has, "%s line %ld: has score %d, want %d", name, line_no, has, want_has);
            if (has && want_has) {
                double p = strtod(expected_text, NULL), c = got;
                double err = fabs(c - p);
                CHECK(err <= MOTION_SCORE_TOLERANCE_REL * p + MOTION_SCORE_TOLERANCE_ABS,
                      "%s line %ld: score %.6g, Python %.6g", name, line_no, c, p);
                if (n_errors < MAX_ERRORS) errors[n_errors++] = p > 0 ? err / p : err;
                int off = abs((int)motion_score_encode((float)c) - (int)motion_score_encode((float)p));
                code_off_by[off < 3 ? off : 3]++;
            }
            packets++;
        } else if (strcmp(kind, "enc") == 0) {
            float score = strtof(strtok(NULL, " \r\n"), NULL);
            unsigned want = (unsigned)strtoul(strtok(NULL, " \r\n"), NULL, 10);
            unsigned got = motion_score_encode(score);
            CHECK(got == want, "line %ld: encode(%.9g) = %u, want %u", line_no, score, got, want);
            enc++;
        } else if (strcmp(kind, "dec") == 0) {
            uint8_t code = (uint8_t)strtoul(strtok(NULL, " \r\n"), NULL, 10);
            char *text = strtok(NULL, " \r\n");
            float got = 0;
            bool has = motion_score_decode(code, &got);
            CHECK(has == (strcmp(text, "-") != 0), "line %ld: decode(%u) has score %d", line_no, code, has);
            if (has) {
                double want = strtod(text, NULL);
                CHECK(fabs(got - want) <= 1e-6 * want, "line %ld: decode(%u) = %.9g, want %.9g", line_no, code,
                      got, want);
            }
            dec++;
        } else {
            CHECK(0, "line %ld: unknown record %s", line_no, kind);
        }
    }
    fclose(f);
    if (cases) report_spread(name, case_from);
    CHECK(cases >= 7 && packets > 1000 && enc >= 100 && dec == 256, "vector file looks truncated");
    printf("vectors: %u cases, %u packets, %u encode and %u decode pairs\n", cases, packets, enc, dec);
    report_spread("all cases", 0);
    printf("  score code off by 0: %u, 1: %u, 2: %u, more: %u\n", code_off_by[0], code_off_by[1], code_off_by[2],
           code_off_by[3]);
    CHECK(code_off_by[3] == 0, "score code more than 2 codes from Python's");
    free(errors);
}

static void push_quiet(motion_score_link_t *s, int64_t t0, int n, int64_t step_us, uint16_t n_sub) {
    float amp[MOTION_SCORE_MAX_SUBCARRIERS];
    for (int i = 0; i < n; i++) {
        for (unsigned k = 0; k < n_sub; k++) amp[k] = 20.0f + (float)((i * 7 + k * 3) % 5);
        motion_score_push(s, t0 + i * step_us, amp, n_sub);
    }
}

static void test_memory_per_link_fits_29_links(void) {
    printf("memory: %zu bytes per link at %d subcarriers; %d links %zu bytes (budget %d)\n",
           sizeof(motion_score_link_t), MOTION_SCORE_MAX_SUBCARRIERS, LINKS_PER_NODE,
           LINKS_PER_NODE * sizeof(motion_score_link_t), MEMORY_BUDGET_BYTES);
    CHECK(LINKS_PER_NODE * sizeof(motion_score_link_t) <= MEMORY_BUDGET_BYTES, "29 links exceed the budget");
}

static void test_subcarrier_count_change_restarts_the_link(void) {
    static motion_score_link_t s;
    float amp[MOTION_SCORE_MAX_SUBCARRIERS] = {0};
    float score;
    motion_score_init(&s);
    push_quiet(&s, 1000000, 30, 50000, 52);
    CHECK(motion_score_get(&s, &score), "no score after 1.5 s at 20 Hz");
    CHECK(motion_score_push(&s, 2600000, amp, 24) == MOTION_SCORE_RESTARTED, "new layout not reported");
    CHECK(!motion_score_get(&s, &score), "score survived a layout change");
}

static void test_bad_arguments_are_refused(void) {
    static motion_score_link_t s;
    float amp[MOTION_SCORE_MAX_SUBCARRIERS] = {0};
    motion_score_init(&s);
    CHECK(motion_score_push(&s, 1, amp, 0) == MOTION_SCORE_BAD_ARG, "n_sub 0 accepted");
    CHECK(motion_score_push(&s, 1, amp, MOTION_SCORE_MAX_SUBCARRIERS + 1) == MOTION_SCORE_BAD_ARG,
          "too many subcarriers accepted");
    CHECK(motion_score_push(&s, 1, NULL, 4) == MOTION_SCORE_BAD_ARG, "NULL amplitudes accepted");
    CHECK(motion_score_push(NULL, 1, amp, 4) == MOTION_SCORE_BAD_ARG, "NULL link accepted");
}

static void test_a_rate_beyond_the_ring_still_scores_and_counts_overflow(void) {
    static motion_score_link_t s;
    float score;
    motion_score_init(&s);
    push_quiet(&s, 1000000, 300, 10000, 16); /* 100 Hz for 3 s */
    CHECK(s.overflowed > 0, "no overflow counted at 100 Hz");
    CHECK(motion_score_get(&s, &score) && score >= 0, "no score at 100 Hz");
}

static void test_reinit_forgets_the_link(void) {
    static motion_score_link_t s;
    float score;
    motion_score_init(&s);
    push_quiet(&s, 1000000, 30, 50000, 8);
    motion_score_init(&s);
    CHECK(!motion_score_get(&s, &score), "score survived init");
    push_quiet(&s, 0, 1, 50000, 8); /* an earlier time is fine after init: a new timeline */
    CHECK(s.dropped == 0, "packet after init dropped");
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s tests/vectors/motion_score.txt\n", argv[0]);
        return 2;
    }
    test_vectors_match_python(argv[1]);
    test_memory_per_link_fits_29_links();
    test_subcarrier_count_change_restarts_the_link();
    test_bad_arguments_are_refused();
    test_a_rate_beyond_the_ring_still_scores_and_counts_overflow();
    test_reinit_forgets_the_link();
    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
