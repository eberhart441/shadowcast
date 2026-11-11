/*
 * The CSI callback hands the node the received frame's body (wifi_csi_info_t.payload): for ESP-NOW, the
 * vendor-specific action frame of the ESP-IDF ESP-NOW docs ("Frame Format"). espnow_body finds the ESP-NOW data in
 * it, so a transceiver gets the round broadcast and its CSI from one callback.
 */
#include <stdio.h>
#include <string.h>

#include "../espnow_frame.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

/* Category 127, OUI 18:fe:34, 4 random bytes, then the vendor-specific element: ID 221, length (OUI + type +
 * version + body), OUI 18:fe:34, type 4, version byte, body. The docs' layout, written out byte by byte. */
static size_t frame(uint8_t *out, const uint8_t *body, uint8_t body_len, uint8_t version)
{
    const uint8_t head[] = {127, 0x18, 0xfe, 0x34, 0xa1, 0xb2, 0xc3, 0xd4, 221, (uint8_t)(body_len + 5), 0x18, 0xfe,
                            0x34, 4, version};
    memcpy(out, head, sizeof head);
    memcpy(out + sizeof head, body, body_len);
    return sizeof head + body_len;
}

int main(void)
{
    uint8_t buf[300], body[250];
    const uint8_t *got;
    size_t got_len;
    for (int i = 0; i < 250; i++) {
        body[i] = (uint8_t)(i * 7);
    }

    size_t n = frame(buf, body, 30, 1);
    CHECK(espnow_body(buf, n, &got, &got_len) && got_len == 30 && memcmp(got, body, 30) == 0, "v1 body found");

    n = frame(buf, body, 30, 2);   /* ESP-NOW v2 sends a short payload in one element, with version 2 */
    CHECK(espnow_body(buf, n, &got, &got_len) && got_len == 30, "v2 single-element body found");

    n = frame(buf, body, 250, 1);
    CHECK(espnow_body(buf, n + 4, &got, &got_len) && got_len == 250, "a trailing FCS after the element is ignored");

    n = frame(buf, body, 0, 1);
    CHECK(espnow_body(buf, n, &got, &got_len) && got_len == 0, "an empty body is a body");

    n = frame(buf, body, 30, 1);
    CHECK(!espnow_body(buf, n - 1, &got, &got_len), "an element longer than the frame is refused");
    CHECK(!espnow_body(buf, 10, &got, &got_len), "a frame shorter than the headers is refused");
    buf[0] = 4;
    CHECK(!espnow_body(buf, n, &got, &got_len), "not a vendor-specific action frame");
    frame(buf, body, 30, 1);
    buf[13] = 5;
    CHECK(!espnow_body(buf, n, &got, &got_len), "a vendor element that is not ESP-NOW (type 4)");
    frame(buf, body, 30, 1);
    buf[2] = 0x00;
    CHECK(!espnow_body(buf, n, &got, &got_len), "an action frame from another vendor");

    printf("espnow_frame: %d failures\n", failures);
    return failures ? 1 : 0;
}
