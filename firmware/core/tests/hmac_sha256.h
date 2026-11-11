/*
 * A small portable HMAC-SHA256 (FIPS 180-4, RFC 2104) for the node-core tests on the PC. The node itself uses
 * mbedTLS (the ESP32-C5's SHA accelerator); this is only the reference the PC tests check round_auth.c with.
 */
#ifndef HMAC_SHA256_H
#define HMAC_SHA256_H

#include <stddef.h>
#include <stdint.h>

void hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t len, uint8_t out[32]);

#endif /* HMAC_SHA256_H */
