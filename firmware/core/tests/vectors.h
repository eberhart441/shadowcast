/* Small helpers for the node-core tests that read vector files: split a line into tokens, parse hex. */
#ifndef VECTORS_H
#define VECTORS_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define VEC_MAX_TOKENS 512

/* Split `line` in place on spaces, tabs and line ends. Returns the number of tokens (at most `max`). */
static inline int vec_split(char *line, char **tok, int max)
{
    int n = 0;
    char *p = line;
    while (*p && n < max) {
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
            *p++ = '\0';
        }
        if (!*p) {
            break;
        }
        tok[n++] = p;
        while (*p && *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') {
            p++;
        }
    }
    return n;
}

static inline int vec_nibble(char c)
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

/* Parse a hex string ("-" is empty) into `out`. Returns the byte count, at most `cap`. */
static inline size_t vec_hex(const char *hex, uint8_t *out, size_t cap)
{
    size_t n = 0;
    if (strcmp(hex, "-") == 0) {
        return 0;
    }
    while (hex[0] && hex[1] && n < cap) {
        int hi = vec_nibble(hex[0]), lo = vec_nibble(hex[1]);
        if (hi < 0 || lo < 0) {
            break;
        }
        out[n++] = (uint8_t)(hi << 4 | lo);
        hex += 2;
    }
    return n;
}

#endif /* VECTORS_H */
