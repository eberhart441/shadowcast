/*
 * The ESP-NOW data inside a received frame body, as the CSI callback hands it over (wifi_csi_info_t.payload).
 *
 * Layout (ESP-IDF docs, ESP-NOW "Frame Format"): category 127, OUI 18:fe:34, 4 random bytes, then a
 * vendor-specific element: ID 221, length (OUI + type + version + body), OUI 18:fe:34, type 4, version, body.
 * Round broadcasts are at most 150 bytes, so they always fit one element (ESP-NOW v1 or v2).
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef ESPNOW_FRAME_H
#define ESPNOW_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ESPNOW_BODY_OFFSET 15

/* Find the body of the first ESP-NOW element in frame[0..len). False if the frame is not ESP-NOW. */
bool espnow_body(const uint8_t *frame, size_t len, const uint8_t **body, size_t *body_len);

#endif /* ESPNOW_FRAME_H */
