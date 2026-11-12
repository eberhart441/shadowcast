/*
 * What the node image's files share (node_main.c: USB, settings, radio and app_main; node_transceiver.c: the
 * transceiver role; node_gateway.c: the gateway role). Private to firmware/node/main.
 */
#ifndef NODE_COMMON_H
#define NODE_COMMON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"

#include "node_settings.h"
#include "round_broadcast.h"

#define QUEUE_DEPTH 32
#define PAYLOAD_COPY (8 + RB_MAX_FRAME_BYTES + 7 * 2 + 16)   /* our longest frame in two ESP-NOW v2 elements */
#define HEARD_MAX RB_MAX_FRAME_BYTES   /* a round broadcast with two raw CSI blocks; anything longer is not ours */
#define RAW_LOG_PERIOD_US 10000000  /* while raw mode is on, a log line this often */
#define GAIN_RECORD_FRAMES 100      /* as csi_recv: record 100 frames, then force their median gain */
#define REARM_TOLERANCE_US 100
#define STATUS_PERIOD_US 1000000
#define NODE_TASK_PRIORITY 6
#define USB_TASK_PRIORITY 3

extern const uint8_t NODE_BROADCAST_MAC[6];
extern node_settings_t g_node_settings;   /* loaded at boot; changed only by a command that then restarts */
extern bool g_node_keyed;                 /* false: a transceiver without the kit key, which only listens */
extern uint8_t g_node_serial[6];          /* base MAC = USB serial = node id */
extern uint8_t g_node_sta_mac[6];

/* node_main.c */
void node_count(int which);                                       /* one more of a GR_COUNT_* status counter */
void node_usb_write(const uint8_t *record, size_t n, TickType_t wait);   /* one gateway record to USB */
void node_add_broadcast_peer(void);                               /* ESP-NOW broadcasts at HT20 MCS0 */

/* node_transceiver.c */
void node_transceiver_start(void);
bool node_round_member(void);   /* a member of a round now (the status record and the OTA health check use it) */

/* node_gateway.c */
void node_gateway_start(void);
bool node_gateway_command(const char *line, char *reply, size_t n);   /* "send <hex>": true if it was one */

#endif
