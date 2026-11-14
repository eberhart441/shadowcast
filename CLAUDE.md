## Project context

What the project is for, where it stands, gotchas, and pointers to every other doc: read `docs/agents/project-context.md` first.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for eberhart441/shadowcast (via the `gh` CLI). Issues `#N` cited in the hackathon docs and ADRs belong to the old repo, CodeMetalForge-Hackathon/Team-12. See `docs/agents/issue-tracker.md`.

### Research

The scaling investigation and the plan for the redo: `docs/research/scaling-redo/00-synthesis.md` (start there; it indexes reports 01-09).

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root + `docs/adr/`. See `docs/agents/domain.md`.

### Hardware and live rig

Which board is on which port: `python scripts/ports.py` (matches USB serials to `config/room.json`, flags unknown boards). Boards, flashing, wireless receivers, thresholds and dashboard restarts: read `docs/agents/hardware.md` first.

### Subagents

Setup, base branch and commit trailer for implementer and merger agents: `docs/agents/implementer-brief.md`.

### Checks

Run `scripts/check.sh` before committing (fast tier); `scripts/check.sh --full` before a merge. See docs/agents/implementer-brief.md.
