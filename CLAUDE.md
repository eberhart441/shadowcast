## Project context

What the project is for, where it stands, gotchas, and pointers to every other doc: read `docs/agents/project-context.md` first.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for CodeMetalForge-Hackathon/Team-12 (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root + `docs/adr/`. See `docs/agents/domain.md`.

### Hardware and live rig

Boards, ports, flashing, thresholds and dashboard restarts: read `docs/agents/hardware.md` first.

### Subagents

Setup, base branch and commit trailer for implementer and merger agents: `docs/agents/implementer-brief.md`.

### Checks

Run `scripts/check.sh` before committing (fast tier); `scripts/check.sh --full` before a merge. See docs/agents/implementer-brief.md.
