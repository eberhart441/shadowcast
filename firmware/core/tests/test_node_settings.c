/*
 * Node settings: role, channel, round rate and slot guard, set at runtime by text commands over USB
 * (scripts/node.py), never by build flags. A set is all or nothing: one bad value leaves every setting as it was.
 * The node index is not a setting any more: the round scheduler gives it out (ticket #14). Settings stored by the
 * first node image (version 1, with index and nodes) keep their role, channel and round rate.
 */
#include <stdio.h>
#include <string.h>

#include "../node_settings.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static node_cmd_t run(node_settings_t *s, const char *line, char *reply)
{
    return node_settings_command(s, "10:BD:A3:CC:85:BC", line, reply, 160);
}

int main(void)
{
    node_settings_t s, before;
    char reply[160];
    node_settings_default(&s);
    CHECK(s.role == NODE_ROLE_TRANSCEIVER && s.channel == 6 && s.round_hz == 40 && s.guard_us == 250,
          "defaults: transceiver, channel 6, 40 Hz, 250 us guard");
    CHECK(node_settings_valid(&s), "the defaults are valid");

    CHECK(run(&s, "show", reply) == NODE_CMD_NONE, "show changes nothing");
    CHECK(strcmp(reply, "ok serial=10:BD:A3:CC:85:BC role=transceiver channel=6 round_hz=40 guard_us=250") == 0,
          "show reply: %s", reply);

    CHECK(run(&s, "set role=gateway channel=11\r\n", reply) == NODE_CMD_SAVE_AND_RESTART, "set saves and restarts");
    CHECK(s.role == NODE_ROLE_GATEWAY && s.channel == 11, "set applied");
    CHECK(strncmp(reply, "ok ", 3) == 0 && strstr(reply, "role=gateway channel=11"), "set reply: %s", reply);

    before = s;
    CHECK(run(&s, "set round_hz=20 channel=14", reply) == NODE_CMD_ERROR, "channel 14 is refused");
    CHECK(memcmp(&s, &before, sizeof s) == 0, "a refused set changes nothing, not even the good values");
    CHECK(strncmp(reply, "error ", 6) == 0 && strstr(reply, "channel"), "the error names the setting: %s", reply);

    CHECK(run(&s, "set index=2", reply) == NODE_CMD_ERROR && strstr(reply, "index") && strstr(reply, "scheduler"),
          "the node index is no longer a setting: %s", reply);
    CHECK(run(&s, "set nodes=8", reply) == NODE_CMD_ERROR, "nor is the member count");
    CHECK(run(&s, "set guard_us=400 round_hz=20", reply) == NODE_CMD_SAVE_AND_RESTART && s.guard_us == 400
              && s.round_hz == 20, "the guard and round rate together");
    CHECK(run(&s, "set guard_us=20", reply) == NODE_CMD_ERROR && strstr(reply, "guard"), "a guard under 50 us");
    CHECK(run(&s, "set guard_us=70000", reply) == NODE_CMD_ERROR && strstr(reply, "guard"), "not a 16-bit number");
    CHECK(run(&s, "set round_hz=255 guard_us=1500", reply) == NODE_CMD_ERROR && strstr(reply, "slot"),
          "a round too fast for two members and the join slot is refused: %s", reply);
    CHECK(run(&s, "set role=sender", reply) == NODE_CMD_ERROR && strstr(reply, "role"), "unknown role");
    CHECK(run(&s, "set colour=blue", reply) == NODE_CMD_ERROR && strstr(reply, "colour"), "unknown setting");
    CHECK(run(&s, "set channel=6x", reply) == NODE_CMD_ERROR, "not a number");
    CHECK(run(&s, "set channel", reply) == NODE_CMD_ERROR, "no value");
    CHECK(run(&s, "set", reply) == NODE_CMD_ERROR, "nothing to set");
    CHECK(run(&s, "restart", reply) == NODE_CMD_RESTART && strncmp(reply, "ok", 2) == 0, "restart");
    CHECK(run(&s, "flash", reply) == NODE_CMD_ERROR && strstr(reply, "show"), "unknown command lists the commands");
    CHECK(run(&s, "", reply) == NODE_CMD_NONE && reply[0] == '\0', "an empty line is ignored");

    s.channel = 0;
    CHECK(!node_settings_valid(&s), "stored settings are checked too");

    node_settings_t loaded;
    const uint8_t v1_gateway[6] = {1, NODE_ROLE_GATEWAY, 11, 0, 2, 20};   /* version, role, channel, index, nodes, hz */
    CHECK(node_settings_load(v1_gateway, sizeof v1_gateway, &loaded) && loaded.role == NODE_ROLE_GATEWAY
              && loaded.channel == 11 && loaded.round_hz == 20 && loaded.guard_us == 250
              && loaded.version == NODE_SETTINGS_VERSION,
          "settings stored by the first image keep their role, channel and round rate");
    node_settings_default(&s);
    s.channel = 3;
    CHECK(node_settings_load(&s, sizeof s, &loaded) && loaded.channel == 3, "current settings load as stored");
    const uint8_t v1_bad[6] = {1, 0, 99, 0, 2, 40};
    CHECK(!node_settings_load(v1_bad, sizeof v1_bad, &loaded) && loaded.channel == 6, "bad ones give the defaults");
    CHECK(!node_settings_load(v1_gateway, 5, &loaded), "a blob of another size gives the defaults");
    printf("node_settings: %d failures\n", failures);
    return failures ? 1 : 0;
}
