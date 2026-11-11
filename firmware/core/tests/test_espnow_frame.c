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

/* ESP-NOW v2 (ESP-IDF docs, "Frame Format"): a payload over 250 bytes is split over several vendor elements, each
 * ID 221, length, OUI, type 4 and a version byte whose bit 4 ("more data") is set on every element but the last. */
static void check_v2_elements(void)
{
    static uint8_t buf[600], body[300], out[416];
    const uint8_t head[] = {127, 0x18, 0xfe, 0x34, 0xa1, 0xb2, 0xc3, 0xd4};
    for (int i = 0; i < 300; i++) {
        body[i] = (uint8_t)(i * 13 + 1);
    }
    size_t n = 0, got = 0;
    memcpy(buf, head, sizeof head);
    n = sizeof head;
    const uint8_t e1[] = {221, 255, 0x18, 0xfe, 0x34, 4, 0x12};   /* 250 bytes of body, more data, version 2 */
    memcpy(buf + n, e1, sizeof e1);
    memcpy(buf + n + sizeof e1, body, 250);
    n += sizeof e1 + 250;
    const uint8_t e2[] = {221, 55, 0x18, 0xfe, 0x34, 4, 0x02};    /* the last 50, no more data */
    memcpy(buf + n, e2, sizeof e2);
    memcpy(buf + n + sizeof e2, body + 250, 50);
    n += sizeof e2 + 50;
    CHECK(espnow_gather(buf, n + 4, out, sizeof out, &got) && got == 300 && memcmp(out, body, 300) == 0,
          "a 300-byte v2 payload over two elements is put back together (%zu bytes)", got);
    CHECK(!espnow_gather(buf, n + 4, out, 299, &got), "a payload longer than the buffer is refused");
    CHECK(!espnow_gather(buf, sizeof head + sizeof e1 + 250, out, sizeof out, &got),
          "an element that says more data follows, with none after it, is refused");
    size_t m = frame(buf, body, 30, 2);
    CHECK(espnow_gather(buf, m, out, sizeof out, &got) && got == 30 && memcmp(out, body, 30) == 0,
          "a single element is just its body");
    buf[0] = 4;
    CHECK(!espnow_gather(buf, m, out, sizeof out, &got), "not ESP-NOW: refused");
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

    check_v2_elements();
    printf("espnow_frame: %d failures\n", failures);
    return failures ? 1 : 0;
}
