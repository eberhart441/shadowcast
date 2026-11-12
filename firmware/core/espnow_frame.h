/*
 * The ESP-NOW data inside a received frame body, as the CSI callback hands it over (wifi_csi_info_t.payload).
 *
 * Layout (ESP-IDF docs, ESP-NOW "Frame Format"): category 127, OUI 18:fe:34, 4 random bytes, then a
 * vendor-specific element: ID 221, length (OUI + type + version + body), OUI 18:fe:34, type 4, version, body.
 * A plain round broadcast is at most 150 bytes, so it fits one element (ESP-NOW v1 or v2). A round broadcast with
 * raw CSI (up to 416 bytes, ticket #17) is longer than v1's 250: ESP-NOW v2 splits it over several elements, each
 * with bit 4 of its version byte ("more data") set except the last; espnow_gather puts the body back together.
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

/* Copy the whole ESP-NOW data of frame[0..len) into out[0..cap): the bodies of its elements, the first one and every
 * one after an element that says more data follows. False if the frame is not ESP-NOW, an element runs past the
 * frame, a promised element is missing, or the data does not fit. */
bool espnow_gather(const uint8_t *frame, size_t len, uint8_t *out, size_t cap, size_t *out_len);

#endif /* ESPNOW_FRAME_H */
