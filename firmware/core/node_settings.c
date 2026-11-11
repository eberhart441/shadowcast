#include "node_settings.h"

#include <stdio.h>
#include <string.h>

#include "fixed_round.h"

#define MAX_TOKEN 32

static const char *const ROLE_NAMES[] = {"transceiver", "gateway"};

void node_settings_default(node_settings_t *s)
{
    memset(s, 0, sizeof *s);
    s->version = NODE_SETTINGS_VERSION;
    s->role = NODE_ROLE_TRANSCEIVER;
    s->channel = 6;
    s->index = 0;
    s->nodes = 2;
    s->round_hz = 40;
}

/* Why s is not valid, or NULL. */
static const char *invalid(const node_settings_t *s)
{
    if (s->role > NODE_ROLE_GATEWAY) {
        return "role must be transceiver or gateway";
    }
    if (s->channel < 1 || s->channel > 13) {
        return "channel must be a 2.4 GHz channel, 1 to 13";
    }
    if (s->nodes < 2 || s->nodes > 32) {
        return "nodes must be 2 to 32";
    }
    if (s->index >= s->nodes) {
        return "index must be below nodes (0 to nodes - 1)";
    }
    if (s->round_hz < 1 || 1000000L / ((long)s->round_hz * s->nodes) < FIXED_ROUND_MIN_SLOT_US) {
        return "round_hz too high: each slot must be at least 1 ms (round_hz x nodes <= 1000)";
    }
    return NULL;
}

bool node_settings_valid(const node_settings_t *s)
{
    return s->version == NODE_SETTINGS_VERSION && invalid(s) == NULL;
}

void node_settings_format(const node_settings_t *s, char *out, size_t n)
{
    snprintf(out, n, "role=%s channel=%u index=%u nodes=%u round_hz=%u",
             s->role <= NODE_ROLE_GATEWAY ? ROLE_NAMES[s->role] : "?", s->channel, s->index, s->nodes, s->round_hz);
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

static bool parse_u8(const char *text, uint8_t *out)
{
    unsigned v = 0;
    if (!*text) {
        return false;
    }
    for (; *text; text++) {
        if (*text < '0' || *text > '9' || v > 255) {
            return false;
        }
        v = v * 10 + (unsigned)(*text - '0');
    }
    if (v > 255) {
        return false;
    }
    *out = (uint8_t)v;
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
    } else if (klen == 5 && strncmp(tok, "index", 5) == 0) {
        field = &s->index;
    } else if (klen == 5 && strncmp(tok, "nodes", 5) == 0) {
        field = &s->nodes;
    } else if (klen == 8 && strncmp(tok, "round_hz", 8) == 0) {
        field = &s->round_hz;
    } else {
        snprintf(why, n, "unknown setting '%.*s' (role, channel, index, nodes, round_hz)", (int)klen, tok);
        return why;
    }
    if (!parse_u8(value, field)) {
        snprintf(why, n, "%.*s must be a number 0 to 255, got '%s'", (int)klen, tok, value);
        return why;
    }
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
        snprintf(reply, n, "error set needs key=value (role, channel, index, nodes, round_hz)");
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
