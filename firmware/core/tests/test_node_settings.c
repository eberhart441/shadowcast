/*
 * Node settings: role, channel, node index, member count and round rate, set at runtime by text commands over USB
 * (scripts/node.py), never by build flags. A set is all or nothing: one bad value leaves every setting as it was.
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
    CHECK(s.role == NODE_ROLE_TRANSCEIVER && s.channel == 6 && s.index == 0 && s.nodes == 2 && s.round_hz == 40,
          "defaults: transceiver, channel 6, index 0 of 2, 40 Hz");
    CHECK(node_settings_valid(&s), "the defaults are valid");

    CHECK(run(&s, "show", reply) == NODE_CMD_NONE, "show changes nothing");
    CHECK(strcmp(reply, "ok serial=10:BD:A3:CC:85:BC role=transceiver channel=6 index=0 nodes=2 round_hz=40") == 0,
          "show reply: %s", reply);

    CHECK(run(&s, "set role=gateway channel=11\r\n", reply) == NODE_CMD_SAVE_AND_RESTART, "set saves and restarts");
    CHECK(s.role == NODE_ROLE_GATEWAY && s.channel == 11, "set applied");
    CHECK(strncmp(reply, "ok ", 3) == 0 && strstr(reply, "role=gateway channel=11"), "set reply: %s", reply);

    before = s;
    CHECK(run(&s, "set index=1 channel=14", reply) == NODE_CMD_ERROR, "channel 14 is refused");
    CHECK(memcmp(&s, &before, sizeof s) == 0, "a refused set changes nothing, not even the good values");
    CHECK(strncmp(reply, "error ", 6) == 0 && strstr(reply, "channel"), "the error names the setting: %s", reply);

    CHECK(run(&s, "set index=2", reply) == NODE_CMD_ERROR && strstr(reply, "index"), "index must be below nodes");
    CHECK(run(&s, "set nodes=3 index=2", reply) == NODE_CMD_SAVE_AND_RESTART && s.nodes == 3 && s.index == 2,
          "both together are fine");
    CHECK(run(&s, "set nodes=8 round_hz=200", reply) == NODE_CMD_ERROR && strstr(reply, "slot"),
          "a round too fast for its slots is refused: %s", reply);
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
    printf("node_settings: %d failures\n", failures);
    return failures ? 1 : 0;
}
