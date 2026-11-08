# 0004. Receivers stay wired; wireless receivers deferred

- **Status:** Superseded by [0019](0019-wireless-receivers-over-esp-now.md)
- **Date:** 2026-11-07
- **Recorded in:** `d836c52` (`docs/agents/project-context.md`, "Decisions and why")
- **Related:** [0003](0003-one-shared-channel-at-40-hz.md), [0012](0012-host-ts-line-stream-seam.md), [0015](0015-stdlib-http-server-loopback-commands.md)

## Context

Each receiver streams CSI over its own USB serial port, so the laptop must be within cable reach and each receiver costs a USB port (README "Scaling up", "Limits"). Forwarding CSI over the air would remove both limits, and it is the first README roadmap item. But a receiver's uploads would share channel 6 with the senders' packets, and on this rig the shared channel is already the binding constraint ([0003](0003-one-shared-channel-at-40-hz.md)).

## Decision

Receivers stay on USB serial for now. Wireless receivers are deferred until a half-day spike settles whether detection quality survives the extra traffic on channel 6.

## Alternatives considered

The options below were weighed the same day in a teammate's design note. Its radio-coexistence arguments come from ESP-IDF documentation, not from measurements on this rig.

- **Forward CSI over UDP through an access point pinned to channel 6** (the README roadmap's design). One radio can only be on one channel, so the access point must sit on the sensing channel. Estimated, never prototyped (Evidence).
- **Forward CSI over ESP-NOW to one board that stays on USB (a gateway).** No new infrastructure, but the relayed frames compete for the same airtime as the senders' packets. A teammate prototyped this afterwards on the unmerged branch `feat/wireless-receivers` (`165ce75`, 2026-11-08).
- **Hop between an access point's channel and channel 6.** Not recommended by the note: it drops CSI during every dwell and adds a second timing domain.

## Consequences

- One USB port per receiver remains a hard limit on scaling.
- The Python side is ready either way: a wireless receiver is one more `(host_ts, line)` stream ([0012](0012-host-ts-line-stream-seam.md)). The gateway prototype on `feat/wireless-receivers` splits the gateway's stream back into one stream per receiver, which is that seam doing its job.
- Field use beyond the bench would also need a portable access point and authenticated or encrypted transport; nothing in today's path is authenticated ([0015](0015-stdlib-http-server-loopback-commands.md)).
- **Revisit when:** a wireless variant lands on `main` with measured packet rates and per-link scores against the wired baseline on the same rig.

## Evidence

All estimates, never prototyped at decision time (`docs/agents/project-context.md`, "Decisions and why"):

- Forwarding over UDP works: about 85% to 90% likely.
- Detection quality holds without retuning: only about 50% to 60% likely, because a receiver's own uploads would share channel 6 with the senders.
- The spike needs a router pinned to channel 6 and power for each receiver.
