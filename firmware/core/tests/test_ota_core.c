/*
 * The node's over-the-air update decisions (firmware/core/ota_core.h):
 *   - maintenance commands decode and encode exactly as the golden fixture the host is pinned to
 *     (tests/fixtures/ota_command.json, flattened by scripts/gen_wire_vectors.py), and their tags verify with
 *     round_auth, so node_auth_accept takes them like any other frame;
 *   - what a node does with a command, what it keeps across the restarts of an update, what it does at boot,
 *     the health check of a new image, the download check and the report line.
 *
 *   test_ota_core <tests/vectors/ota_command.txt>
 */
#define _CRT_SECURE_NO_WARNINGS /* the MSVC runtime (clang on Windows) deprecates fopen */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../ota_core.h"
#include "../round_auth.h"
#include "hmac_sha256.h"
#include "vectors.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static uint8_t s_key[RB_AUTH_KEY_BYTES];

static bool reference_mac(void *ctx, const uint8_t *msg, size_t len, uint8_t mac[RB_AUTH_MAC_BYTES])
{
    hmac_sha256((const uint8_t *)ctx, RB_AUTH_KEY_BYTES, msg, len, mac);
    return true;
}

/* ---------------------------------------------------------------- codec, from the vectors */

static void check_valid(rb_auth_t *a, char **tok, int n)
{
    const char *name = tok[1];
    uint8_t bytes[OTA_MAX_BYTES + 8], again[OTA_MAX_BYTES + 8], sha[OTA_SHA_BYTES], tag[8];
    size_t len = vec_hex(tok[2], bytes, sizeof bytes);
    ota_command_t c;
    CHECK(ota_is_command(bytes, len), "%s: not recognised as a maintenance command", name);
    ota_status_t st = ota_command_decode(bytes, len, &c);
    CHECK(st == OTA_OK, "%s: decode gives %s", name, ota_status_name(st));
    if (st != OTA_OK) {
        return;
    }
    CHECK(c.round == strtoul(tok[3], NULL, 10), "%s: round %u", name, c.round);
    CHECK(c.command == strtoul(tok[4], NULL, 10), "%s: command %u", name, c.command);
    CHECK(c.port == strtoul(tok[5], NULL, 10), "%s: port %u", name, c.port);
    CHECK(c.ap_channel == strtoul(tok[6], NULL, 10), "%s: ap_channel %u", name, c.ap_channel);
    CHECK(c.issued == strtoul(tok[7], NULL, 10), "%s: issued %lu", name, (unsigned long)c.issued);
    CHECK(c.image_size == strtoul(tok[8], NULL, 10), "%s: image_size %lu", name, (unsigned long)c.image_size);
    vec_hex(tok[9], sha, sizeof sha);
    CHECK(memcmp(c.image_sha, sha, sizeof sha) == 0, "%s: image sha", name);
    vec_hex(tok[10], tag, sizeof tag);
    CHECK(memcmp(c.tag, tag, sizeof tag) == 0, "%s: tag", name);
    int targets = atoi(tok[11]);
    CHECK(c.n_targets == targets && n == 12 + targets, "%s: %u targets", name, c.n_targets);
    for (int i = 0; i < targets && i < OTA_MAX_TARGETS && 12 + i < n; i++) {
        uint8_t serial[OTA_SERIAL_BYTES];
        vec_hex(tok[12 + i], serial, sizeof serial);
        CHECK(memcmp(c.targets[i], serial, sizeof serial) == 0, "%s: target %d", name, i);
    }
    static const uint8_t zero[OTA_SERIAL_BYTES] = {0};
    CHECK(memcmp(c.sender, zero, sizeof zero) == 0, "%s: the host's id is all zero", name);
    size_t m = ota_command_encode(&c, again, sizeof again);
    CHECK(m == len && memcmp(again, bytes, len) == 0, "%s: encoding the decoded command does not give the bytes", name);
    CHECK(rb_auth_tag_ok(a, bytes, len), "%s: the tag does not verify with round_auth", name);
    CHECK(ota_command_encode(&c, again, len - 1) == 0, "%s: encodes into a buffer that is too small", name);
}

static void check_invalid(char **tok)
{
    uint8_t bytes[OTA_MAX_BYTES + 32];
    size_t len = vec_hex(tok[2], bytes, sizeof bytes);
    ota_command_t c;
    ota_status_t st = ota_command_decode(bytes, len, &c);
    CHECK(strcmp(ota_status_name(st), tok[3]) == 0, "%s: decode gives %s, expected %s", tok[1], ota_status_name(st),
          tok[3]);
}

static void check_forged(rb_auth_t *a, char **tok)
{
    uint8_t bytes[OTA_MAX_BYTES + 8];
    size_t len = vec_hex(tok[2], bytes, sizeof bytes);
    ota_command_t c;
    CHECK(ota_command_decode(bytes, len, &c) == OTA_OK, "%s: a forged command should still decode", tok[1]);
    CHECK(!rb_auth_tag_ok(a, bytes, len), "%s: a forged command verifies", tok[1]);
}

static void run_vectors(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(2);
    }
    rb_auth_t a;
    rb_auth_init(&a, reference_mac, s_key);
    static char line[4096];
    char *tok[VEC_MAX_TOKENS];
    int cases = 0;
    while (fgets(line, sizeof line, f)) {
        int n = vec_split(line, tok, VEC_MAX_TOKENS);
        if (n == 0 || tok[0][0] == '#') {
            continue;
        }
        if (strcmp(tok[0], "key") == 0) {
            vec_hex(tok[1], s_key, sizeof s_key);
        } else if (strcmp(tok[0], "valid") == 0) {
            check_valid(&a, tok, n);
            cases++;
        } else if (strcmp(tok[0], "invalid") == 0) {
            check_invalid(tok);
            cases++;
        } else if (strcmp(tok[0], "forged") == 0) {
            check_forged(&a, tok);
            cases++;
        }
    }
    fclose(f);
    CHECK(cases >= 15, "only %d vector cases read from %s", cases, path);
    uint8_t round_broadcast[RB_HEADER_BYTES] = {2, 1};
    CHECK(!ota_is_command(round_broadcast, sizeof round_broadcast), "a round broadcast is taken for a command");
}

/* ---------------------------------------------------------------- what a node does with a command */

static const uint8_t ME[OTA_SERIAL_BYTES] = {0x02, 0, 0, 0, 0x02, 0x5c};
static const uint8_t OTHER[OTA_SERIAL_BYTES] = {0x02, 0, 0, 0, 0x01, 0x5c};

static ota_command_t command(uint8_t which, uint32_t issued, uint8_t ap_channel)
{
    ota_command_t c;
    memset(&c, 0, sizeof c);
    c.command = which;
    c.port = 8070;
    c.ap_channel = ap_channel;
    c.issued = issued;
    c.image_size = which == OTA_CMD_UPDATE ? 0x0E1000 : 0;
    memset(c.image_sha, which == OTA_CMD_UPDATE ? 0xab : 0, OTA_SHA_BYTES);
    c.n_targets = 2;
    memcpy(c.targets[0], OTHER, OTA_SERIAL_BYTES);
    memcpy(c.targets[1], ME, OTA_SERIAL_BYTES);
    return c;
}

static void check_actions(void)
{
    ota_command_t up = command(OTA_CMD_UPDATE, 1000, 36), rep = command(OTA_CMD_REPORT, 1000, 36);
    bool already = true;
    CHECK(ota_command_action(&up, ME, 0, 6, NULL, &already) == OTA_ACT_UPDATE && !already,
          "a target of an update with no command run yet does not update");
    CHECK(ota_command_action(&rep, ME, 999, 6, NULL, NULL) == OTA_ACT_REPORT, "a target of a report does not report");
    uint8_t stranger[OTA_SERIAL_BYTES] = {0x02, 0, 0, 0, 0x03, 0x5c};
    CHECK(ota_command_action(&up, stranger, 0, 6, NULL, NULL) == OTA_ACT_NONE, "a node that is no target updates");
    CHECK(ota_command_action(&up, ME, 1000, 6, NULL, NULL) == OTA_ACT_NONE, "a command already run is run again");
    CHECK(ota_command_action(&up, ME, 1001, 6, NULL, NULL) == OTA_ACT_NONE, "an older command than the last is run");

    uint8_t sha[OTA_SHA_BYTES];
    memset(sha, 0xab, sizeof sha);
    CHECK(ota_command_action(&up, ME, 0, 6, sha, &already) == OTA_ACT_REPORT && already,
          "an update to the image that already runs is not a report of \"already\"");
    sha[31] ^= 1;
    CHECK(ota_command_action(&up, ME, 0, 6, sha, &already) == OTA_ACT_UPDATE && !already,
          "an update to another image than the running one is skipped");

    /* the maintenance network must not overlap the 20 MHz sensing channel */
    CHECK(ota_channels_clear(6, 36) && ota_channels_clear(6, 149) && ota_channels_clear(6, 177),
          "a 5 GHz network is taken for overlapping 2.4 GHz channel 6");
    CHECK(ota_channels_clear(6, 1) && ota_channels_clear(6, 11) && ota_channels_clear(1, 13),
          "a 2.4 GHz network 5 or more channels away is refused");
    CHECK(!ota_channels_clear(6, 6) && !ota_channels_clear(6, 2) && !ota_channels_clear(6, 10)
              && !ota_channels_clear(13, 14), "an overlapping 2.4 GHz network is allowed");
    CHECK(ota_channels_clear(6, 0), "channel 0 (scan for the network) is refused before the scan");
    CHECK(!ota_channels_clear(6, 15) && !ota_channels_clear(6, 35) && !ota_channels_clear(6, 178),
          "a channel that does not exist is allowed");
    ota_command_t near = command(OTA_CMD_UPDATE, 1000, 8);
    CHECK(ota_command_action(&near, ME, 0, 6, NULL, NULL) == OTA_ACT_NONE,
          "an update over a network that overlaps the sensing channel is run");
}

/* ---------------------------------------------------------------- the record and the boot decision */

/* Store and load, as the glue does at every step (NVS keeps only the bytes). */
static ota_record_t stored(const ota_record_t *r)
{
    uint8_t blob[OTA_RECORD_BYTES + 4];
    ota_record_t back;
    size_t n = ota_record_encode(r, blob, sizeof blob);
    CHECK(n == OTA_RECORD_BYTES, "a record encodes to %zu bytes", n);
    CHECK(ota_record_decode(blob, n, &back), "a stored record does not load");
    return back;
}

static void check_record_blob(void)
{
    ota_record_t r, back;
    uint8_t blob[OTA_RECORD_BYTES];
    ota_record_init(&r);
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_RUN, "a node with no record does not just run");
    ota_record_encode(&r, blob, sizeof blob);
    blob[0] = OTA_RECORD_VERSION + 1;
    CHECK(!ota_record_decode(blob, sizeof blob, &back) && back.job == OTA_JOB_NONE && back.attempt == 0,
          "a record of another version loads");
    CHECK(!ota_record_decode(blob, sizeof blob - 1, &back), "a record of another length loads");
    CHECK(ota_record_encode(&r, blob, sizeof blob - 1) == 0, "a record encodes into a buffer that is too small");
}

/* The update from command to judged attempt. Returns the record as stored after the health check's verdict. */
static ota_record_t updated_and_booted(void)
{
    ota_record_t r;
    ota_record_init(&r);
    ota_command_t up = command(OTA_CMD_UPDATE, 1000, 36);
    ota_record_take(&r, &up, OTA_ACT_UPDATE, false);
    r = stored(&r);
    CHECK(r.last_issued == 1000, "the command's time is not remembered");
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_MAINTENANCE && r.job == OTA_JOB_UPDATE && r.port == 8070
              && r.ap_channel == 36 && r.image_size == 0x0E1000 && r.image_sha[0] == 0xab,
          "a stored update job does not boot into maintenance mode with its image");
    uint8_t good[OTA_SHA_BYTES], bad[OTA_SHA_BYTES];
    memset(good, 0xab, sizeof good);
    memcpy(bad, good, sizeof bad);
    bad[7] ^= 0x40;
    CHECK(ota_download_check(&r, 0x0E1000, good) == OTA_OUT_NONE, "the commanded image is refused");
    CHECK(ota_download_check(&r, 0x0E1000, bad) == OTA_OUT_REFUSED_DIGEST, "a corrupt download is not refused");
    CHECK(ota_download_check(&r, 0x0E0FFF, good) == OTA_OUT_REFUSED_SIZE, "a short download is not refused");
    ota_record_job_done(&r, OTA_OUT_NONE, 1);     /* written to slot 1 and selected */
    r = stored(&r);
    CHECK(r.job == OTA_JOB_NONE && r.attempt == 1 && r.attempt_slot == 1, "the attempt is not set");
    CHECK(ota_boot(&r, true, 1) == OTA_BOOT_HEALTH_CHECK, "a new image does not run its health check");
    return r;
}

static void check_update_cycle(void)
{
    uint8_t sha[OTA_SHA_BYTES];
    memset(sha, 0xab, sizeof sha);

    /* the health check passes: committed, reported, then a normal node that knows its image */
    ota_record_t r = updated_and_booted();
    ota_record_committed(&r);
    r = stored(&r);
    CHECK(ota_boot(&r, false, 1) == OTA_BOOT_MAINTENANCE && r.job == OTA_JOB_REPORT
              && r.outcome == OTA_OUT_COMMITTED, "a committed image is not reported");
    CHECK(memcmp(r.running_sha, sha, sizeof sha) == 0, "a committed image is not known as the running one");
    ota_record_job_done(&r, OTA_OUT_COMMITTED, 0);
    r = stored(&r);
    CHECK(ota_boot(&r, false, 1) == OTA_BOOT_RUN && r.job == OTA_JOB_NONE, "after the report the node does not run");
    ota_command_t again = command(OTA_CMD_UPDATE, 2000, 36);
    bool already = false;
    CHECK(ota_command_action(&again, ME, r.last_issued, 6, r.running_sha, &already) == OTA_ACT_REPORT && already,
          "a second update to the committed image updates again");

    /* the health check fails: rolled back and reported by the old image */
    r = updated_and_booted();
    ota_record_rolled_back(&r);
    r = stored(&r);
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_MAINTENANCE && r.job == OTA_JOB_REPORT
              && r.outcome == OTA_OUT_ROLLED_BACK, "a failed health check is not reported as a rollback");
    CHECK(r.running_sha[0] == 0, "a rolled-back image is taken as the running one");

    /* the new image crashed before its verdict: the bootloader rolled back, the old image finds the attempt */
    r = updated_and_booted();
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_MAINTENANCE && r.outcome == OTA_OUT_ROLLED_BACK && r.attempt == 0,
          "a crash of the new image is not reported as a rollback");

    /* the image was marked valid but the node restarted before it stored that: still committed */
    r = updated_and_booted();
    CHECK(ota_boot(&r, false, 1) == OTA_BOOT_MAINTENANCE && r.outcome == OTA_OUT_COMMITTED,
          "a valid new image whose verdict was not stored is reported as a rollback");

    /* refused download: reported, nothing attempted, the node runs on */
    ota_record_init(&r);
    ota_command_t up = command(OTA_CMD_UPDATE, 1000, 36);
    ota_record_take(&r, &up, OTA_ACT_UPDATE, false);
    ota_record_job_done(&r, OTA_OUT_REFUSED_IMAGE, 1);
    r = stored(&r);
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_RUN && r.attempt == 0, "a refused image is attempted");

    /* report commands */
    ota_record_init(&r);
    ota_command_t rep = command(OTA_CMD_REPORT, 1500, 0);
    ota_record_take(&r, &rep, OTA_ACT_REPORT, false);
    CHECK(ota_boot(&r, false, 0) == OTA_BOOT_MAINTENANCE && r.job == OTA_JOB_REPORT
              && r.outcome == OTA_OUT_CHECKED_IN && r.last_issued == 1500, "a report command does not check in");
    ota_record_take(&r, &again, OTA_ACT_REPORT, true);
    CHECK(r.outcome == OTA_OUT_ALREADY, "an update to the running image does not report \"already\"");
}

/* ---------------------------------------------------------------- the health check */

#define S 1000000LL

static void check_health(void)
{
    ota_health_t h;
    /* a transceiver that joins and hears and sends enough before the deadline passes */
    ota_health_init(&h, 5 * S, false);
    CHECK(ota_health_step(&h, 6 * S, false, 0, 0) == OTA_HEALTH_WAIT, "a node that just booted is judged");
    CHECK(ota_health_step(&h, 20 * S, true, 19, 40) == OTA_HEALTH_WAIT, "19 heard broadcasts pass");
    CHECK(ota_health_step(&h, 21 * S, true, 40, 19) == OTA_HEALTH_WAIT, "19 sent broadcasts pass");
    CHECK(ota_health_step(&h, 22 * S, false, 40, 40) == OTA_HEALTH_WAIT, "a node that is not a member passes");
    CHECK(ota_health_step(&h, 23 * S, true, 20, 20) == OTA_HEALTH_PASS, "a healthy member does not pass");
    CHECK(ota_health_step(&h, 500 * S, false, 20, 20) == OTA_HEALTH_PASS, "a pass does not stick");

    /* one that never gets there fails at the deadline, counted from boot, and the failure sticks */
    ota_health_init(&h, 5 * S, false);
    CHECK(ota_health_step(&h, 5 * S + OTA_HEALTH_DEADLINE_US - 1, true, 19, 100) == OTA_HEALTH_WAIT,
          "a node fails before the deadline");
    CHECK(ota_health_step(&h, 5 * S + OTA_HEALTH_DEADLINE_US, true, 19, 100) == OTA_HEALTH_FAIL,
          "a node that never heard enough does not fail at the deadline");
    CHECK(ota_health_step(&h, 5 * S + OTA_HEALTH_DEADLINE_US + 1, true, 100, 100) == OTA_HEALTH_FAIL,
          "a failure does not stick");

    /* a gateway only listens: frames heard are its health */
    ota_health_init(&h, 0, true);
    CHECK(ota_health_step(&h, 10 * S, false, 19, 0) == OTA_HEALTH_WAIT, "a gateway that heard 19 frames passes");
    CHECK(ota_health_step(&h, 11 * S, false, 20, 0) == OTA_HEALTH_PASS, "a gateway that hears frames does not pass");
}

/* ---------------------------------------------------------------- the report line */

static void check_report(void)
{
    uint8_t sha[OTA_SHA_BYTES];
    memset(sha, 0xab, sizeof sha);
    ota_report_t rep = {.issued = 1794000000, .outcome = OTA_OUT_COMMITTED, .version = "v0.3-18-g1a2b3c4",
                        .slot = 1, .running_sha = sha};
    memcpy(rep.serial, ME, sizeof ME);
    char line[200];
    size_t n = ota_report_format(&rep, line, sizeof line);
    const char *want = "ota-report 1 serial=02:00:00:00:02:5C issued=1794000000 outcome=committed slot=ota_1 "
                       "image=abababababababab version=v0.3-18-g1a2b3c4";
    CHECK(n == strlen(want) && strcmp(line, want) == 0, "report line: got '%s', want '%s'", line, want);
    rep.running_sha = NULL;
    rep.outcome = OTA_OUT_REFUSED_IMAGE;
    rep.slot = 0;
    rep.version = "has space";
    ota_report_format(&rep, line, sizeof line);
    CHECK(strcmp(line, "ota-report 1 serial=02:00:00:00:02:5C issued=1794000000 outcome=refused_image slot=ota_0 "
                       "image=- version=has_space") == 0, "report line without an image: %s", line);
    CHECK(ota_report_format(&rep, line, 40) == 0, "a report fits into 40 bytes");

    static const char *const names[] = {"none", "committed", "rolled_back", "already", "checked_in", "no_network",
                                        "overlap", "no_server", "refused_size", "refused_digest", "refused_image",
                                        "write_failed"};
    CHECK(sizeof names / sizeof names[0] == OTA_OUT_COUNT, "outcome count");
    for (int i = 0; i < OTA_OUT_COUNT; i++) {
        CHECK(strcmp(ota_outcome_name((ota_outcome_t)i), names[i]) == 0, "outcome %d is %s", i,
              ota_outcome_name((ota_outcome_t)i));
    }
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: test_ota_core <ota_command.txt>\n");
        return 2;
    }
    run_vectors(argv[1]);
    check_actions();
    check_record_blob();
    check_update_cycle();
    check_health();
    check_report();
    if (failures) {
        fprintf(stderr, "test_ota_core: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_ota_core: ok\n");
    return 0;
}
