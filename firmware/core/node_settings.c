#include "node_settings.h"

#include <stdio.h>
#include <string.h>

#include "round_scheduler.h"

#define MAX_TOKEN 32

static const char *const ROLE_NAMES[] = {"transceiver", "gateway"};

void node_settings_default(node_settings_t *s)
{
    memset(s, 0, sizeof *s);
    s->version = NODE_SETTINGS_VERSION;
    s->role = NODE_ROLE_TRANSCEIVER;
    s->channel = 6;
    s->round_hz = 40;
    s->guard_us = RS_GUARD_DEFAULT_US;
}

bool node_channel_is_5ghz(uint8_t channel)
{
    if (channel >= 36 && channel <= 64) {
        return channel % 4 == 0;
    }
    if (channel >= 100 && channel <= 144) {
        return channel % 4 == 0;
    }
    return channel >= 149 && channel <= 177 && (channel - 149) % 4 == 0;
}

bool node_channel_valid(uint8_t channel)
{
    return (channel >= 1 && channel <= 13) || node_channel_is_5ghz(channel);
}

/* Why s is not valid, or NULL. */
static const char *invalid(const node_settings_t *s)
{
    if (s->role > NODE_ROLE_GATEWAY) {
        return "role must be transceiver or gateway";
    }
    if (!node_channel_valid(s->channel)) {
        return "channel must be 1 to 13 (2.4 GHz) or a 20 MHz 5 GHz channel (36..64, 100..144, 149..177 in steps of 4)";
    }
    if (s->guard_us < NODE_GUARD_MIN_US || s->guard_us > NODE_GUARD_MAX_US) {
        return "guard_us must be 50 to 5000";
    }
    if (s->round_hz < 1 || rs_max_members(s->round_hz, s->guard_us) < 2) {
        return "round_hz too high: the round must hold two slots of at least 1 ms (and the guard) and the join slot";
    }
    return NULL;
}

bool node_settings_valid(const node_settings_t *s)
{
    return s->version == NODE_SETTINGS_VERSION && invalid(s) == NULL;
}

bool node_settings_load(const void *blob, size_t n, node_settings_t *s)
{
    const uint8_t *b = blob;
    node_settings_t got;
    node_settings_default(&got);
    if (n == sizeof got && b[0] == NODE_SETTINGS_VERSION) {
        memcpy(&got, blob, sizeof got);
    } else if (n == 6 && b[0] == 1) {   /* version 1: version, role, channel, index, nodes, round_hz */
        got.role = b[1];
        got.channel = b[2];
        got.round_hz = b[5];
    } else {
        node_settings_default(s);
        return false;
    }
    if (!node_settings_valid(&got)) {
        node_settings_default(s);
        return false;
    }
    *s = got;
    return true;
}

void node_settings_format(const node_settings_t *s, char *out, size_t n)
{
    snprintf(out, n, "role=%s channel=%u round_hz=%u guard_us=%u",
             s->role <= NODE_ROLE_GATEWAY ? ROLE_NAMES[s->role] : "?", s->channel, s->round_hz, s->guard_us);
}

/* Copy the next space-separated token of *p into tok (at most MAX_TOKEN - 1 chars). Returns false at the end. */
static bool next_token(const char **p, char *tok)
{
    while (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n') {
        (*p)++;
    }
    if (!**p) {
        return false;
    }
    size_t n = 0;
    while (**p && **p != ' ' && **p != '\t' && **p != '\r' && **p != '\n') {
        if (n < MAX_TOKEN - 1) {
            tok[n++] = **p;
        }
        (*p)++;
    }
    tok[n] = '\0';
    return true;
}

static bool parse_number(const char *text, unsigned max, unsigned *out)
{
    unsigned long v = 0;
    if (!*text) {
        return false;
    }
    for (; *text; text++) {
        if (*text < '0' || *text > '9' || v > max) {
            return false;
        }
        v = v * 10 + (unsigned long)(*text - '0');
    }
    if (v > max) {
        return false;
    }
    *out = (unsigned)v;
    return true;
}

/* Apply key=value to s. Returns NULL or the error. */
static const char *apply(node_settings_t *s, const char *tok, char *why, size_t n)
{
    const char *eq = strchr(tok, '=');
    if (!eq) {
        snprintf(why, n, "expected key=value, got '%s'", tok);
        return why;
    }
    size_t klen = (size_t)(eq - tok);
    const char *value = eq + 1;
    uint8_t *field = NULL;
    unsigned v;
    if (klen == 4 && strncmp(tok, "role", 4) == 0) {
        for (uint8_t r = 0; r <= NODE_ROLE_GATEWAY; r++) {
            if (strcmp(value, ROLE_NAMES[r]) == 0) {
                s->role = r;
                return NULL;
            }
        }
        snprintf(why, n, "role must be transceiver or gateway, got '%s'", value);
        return why;
    }
    if (klen == 7 && strncmp(tok, "channel", 7) == 0) {
        field = &s->channel;
    } else if (klen == 8 && strncmp(tok, "round_hz", 8) == 0) {
        field = &s->round_hz;
    } else if (klen == 8 && strncmp(tok, "guard_us", 8) == 0) {
        if (!parse_number(value, 65535, &v)) {
            snprintf(why, n, "guard_us must be a number 0 to 65535, got '%s'", value);
            return why;
        }
        s->guard_us = (uint16_t)v;
        return NULL;
    } else if ((klen == 5 && strncmp(tok, "index", 5) == 0) || (klen == 5 && strncmp(tok, "nodes", 5) == 0)) {
        snprintf(why, n, "'%.*s' is not a setting any more: the round scheduler gives out node indices", (int)klen,
                 tok);
        return why;
    } else {
        snprintf(why, n, "unknown setting '%.*s' (role, channel, round_hz, guard_us)", (int)klen, tok);
        return why;
    }
    if (!parse_number(value, 255, &v)) {
        snprintf(why, n, "%.*s must be a number 0 to 255, got '%s'", (int)klen, tok, value);
        return why;
    }
    *field = (uint8_t)v;
    return NULL;
}

node_cmd_t node_settings_command(node_settings_t *s, const char *serial, const char *line, char *reply, size_t n)
{
    char tok[MAX_TOKEN], text[96];
    const char *p = line;
    reply[0] = '\0';
    if (!next_token(&p, tok)) {
        return NODE_CMD_NONE;
    }
    if (strcmp(tok, "show") == 0) {
        node_settings_format(s, text, sizeof text);
        snprintf(reply, n, "ok serial=%s %s", serial, text);
        return NODE_CMD_NONE;
    }
    if (strcmp(tok, "restart") == 0) {
        snprintf(reply, n, "ok restarting");
        return NODE_CMD_RESTART;
    }
    if (strcmp(tok, "set") != 0) {
        snprintf(reply, n, "error unknown command '%s' (show, set key=value ..., restart)", tok);
        return NODE_CMD_ERROR;
    }
    node_settings_t next = *s;
    int count = 0;
    char why[96];
    while (next_token(&p, tok)) {
        const char *err = apply(&next, tok, why, sizeof why);
        if (err) {
            snprintf(reply, n, "error %s", err);
            return NODE_CMD_ERROR;
        }
        count++;
    }
    if (!count) {
        snprintf(reply, n, "error set needs key=value (role, channel, round_hz, guard_us)");
        return NODE_CMD_ERROR;
    }
    const char *err = invalid(&next);
    if (err) {
        snprintf(reply, n, "error %s", err);
        return NODE_CMD_ERROR;
    }
    *s = next;
    node_settings_format(s, text, sizeof text);
    snprintf(reply, n, "ok %s (saved; restarting)", text);
    return NODE_CMD_SAVE_AND_RESTART;
}
