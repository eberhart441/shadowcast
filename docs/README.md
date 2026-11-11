# Documentation

The top-level [README](../README.md) says what the project is and how to try it. These documents hold the
detail: how the rig is built, how to operate it, how the detection works, and why it is built this way.

## Guides

- [hardware.md](hardware.md): Hardware rig and firmware. The boards and links, adding senders and
  receivers, flashing with `scripts/flash.sh`, and finding each board's serial port.
- [operations.md](operations.md): Operating the system. Calibrating, the live dashboard and its HTTP API,
  the terminal tools, recording and evaluating sessions, troubleshooting, and every command-line flag.
- [theory.md](theory.md): Signal model and detection math. Why movement changes the channel, and the
  math behind the motion score, the thresholds and the heatmap.
- [architecture.md](architecture.md): Architecture and engineering. How the pipeline, modules and
  server fit together, and the engineering choices behind them.
- [../firmware/README.md](../firmware/README.md): The vendored ESP32 firmware. Our edits to Espressif's
  `esp-csi` examples, and building and flashing by hand with `idf.py`.

## Reference

- [../GLOSSARY.md](../GLOSSARY.md): Glossary. The project's terms (link, score, threshold, gain
  baseline and the rest), defined once.
- [round-broadcast.md](round-broadcast.md): Round broadcast format, version 1. The bytes a transceiver
  sends each round (round header and link reports), defined once for the host and the firmware.
- [gateway-record.md](gateway-record.md): Gateway record format, version 1. The framed binary records a
  node writes to its USB port (heard round broadcasts, status, text), defined once for the host and the firmware.
- [rig-checks/two-nodes.md](rig-checks/two-nodes.md): Rig check for two transceivers and a gateway
  (ticket #13): building and setting the one node image, and the walk-between test.
- [adr/README.md](adr/README.md): Architecture decision records. The index of the decisions recorded in
  [`adr/`](adr/).

## Agent notes

Working notes for the coding agents that maintain this repository, in [`agents/`](agents/). People
working on the code will find them useful too.

- [agents/project-context.md](agents/project-context.md): Read first. What the project is for, where it
  stands, the decisions behind it, and what bites.
- [agents/hardware.md](agents/hardware.md): Rules for boards, ports, firmware, thresholds and the running
  dashboard, with the evidence behind them.
- [agents/implementer-brief.md](agents/implementer-brief.md): Setup, base branch, checks and conventions
  for agents that implement or merge work.
- [agents/issue-tracker.md](agents/issue-tracker.md): How issues and specs are kept in GitHub Issues with
  the `gh` CLI.
- [agents/triage-labels.md](agents/triage-labels.md): The triage label vocabulary used on issues.
- [agents/domain.md](agents/domain.md): How the glossary and decision records are laid out and consumed.
