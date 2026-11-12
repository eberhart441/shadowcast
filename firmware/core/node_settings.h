/*
 * Node settings: everything that differs between boards running the one image, set at runtime and kept in flash
 * (NVS) by the node, never by a build flag. The node's identity is not a setting: it is the board serial (base MAC).
 *
 * Settings are changed by one-line text commands written to the node's USB port (scripts/node.py does this):
 *
 *     show                          reply "ok serial=... role=... channel=... round_hz=... guard_us=..."
 *     set key=value [key=value...]  all or nothing; on success the node saves and restarts
 *     restart
 *
 * Replies go back as text records (docs/gateway-record.md): "ok ..." or "error ...".
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef NODE_SETTINGS_H
#define NODE_SETTINGS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define NODE_SETTINGS_VERSION 2   /* 1 was the fixed round's (index and nodes); node_settings_load converts it */
#define NODE_GUARD_MIN_US 50
#define NODE_GUARD_MAX_US 5000

typedef enum { NODE_ROLE_TRANSCEIVER = 0, NODE_ROLE_GATEWAY = 1 } node_role_t;

typedef struct {
    uint8_t version;     /* NODE_SETTINGS_VERSION: a stored blob of another version is ignored */
    uint8_t role;        /* node_role_t */
    uint8_t channel;     /* kit-wide: 2.4 GHz 1..13 (default 6), or a 20 MHz 5 GHz channel (node_channel_valid) */
    uint8_t round_hz;    /* rounds per second, kit-wide */
    uint16_t guard_us;   /* slot guard of the round scheduler, kit-wide */
} node_settings_t;

typedef enum {
    NODE_CMD_NONE = 0,           /* nothing to do (show, empty line) */
    NODE_CMD_SAVE_AND_RESTART,   /* settings changed: save them, then restart */
    NODE_CMD_RESTART,
    NODE_CMD_ERROR,              /* refused; nothing changed */
} node_cmd_t;

/* True for a channel a node senses on: 2.4 GHz 1..13, or a 20 MHz 5 GHz channel of the ESP32-C5 (36..64 and
 * 100..144 in steps of 4, 149..177 in steps of 4). Which band the radio uses follows from it (node_channel_is_5ghz). */
bool node_channel_valid(uint8_t channel);

/* True for a valid 5 GHz channel (36 and up). */
bool node_channel_is_5ghz(uint8_t channel);

/* Transceiver, channel 6, 40 rounds per second, 250 us guard. */
void node_settings_default(node_settings_t *s);

/* Every value in range, and a round that holds at least two members (rs_max_members). */
bool node_settings_valid(const node_settings_t *s);

/* The settings in a stored blob of n bytes: this version, or version 1 (its role, channel and round rate kept).
 * Returns false, with the defaults in *s, when the blob is neither or holds invalid settings. */
bool node_settings_load(const void *blob, size_t n, node_settings_t *s);

/* "role=transceiver channel=6 round_hz=40 guard_us=250" */
void node_settings_format(const node_settings_t *s, char *out, size_t n);

/* Run one command line. `serial` is the board serial, for show. The reply (without a line end) goes to reply. */
node_cmd_t node_settings_command(node_settings_t *s, const char *serial, const char *line, char *reply, size_t n);

#endif /* NODE_SETTINGS_H */
