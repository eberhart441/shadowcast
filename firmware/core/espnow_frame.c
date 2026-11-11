#include "espnow_frame.h"

#define CATEGORY_VENDOR 127
#define ELEMENT_VENDOR 221
#define TYPE_ESPNOW 4

static bool espressif_oui(const uint8_t *p)
{
    return p[0] == 0x18 && p[1] == 0xfe && p[2] == 0x34;
}

bool espnow_body(const uint8_t *frame, size_t len, const uint8_t **body, size_t *body_len)
{
    if (len < ESPNOW_BODY_OFFSET || frame[0] != CATEGORY_VENDOR || !espressif_oui(frame + 1)
            || frame[8] != ELEMENT_VENDOR || !espressif_oui(frame + 10) || frame[13] != TYPE_ESPNOW || frame[9] < 5) {
        return false;
    }
    size_t n = (size_t)frame[9] - 5;   /* the element length covers OUI, type and version too */
    if (ESPNOW_BODY_OFFSET + n > len) {
        return false;
    }
    *body = frame + ESPNOW_BODY_OFFSET;
    *body_len = n;
    return true;
}
