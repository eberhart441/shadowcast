# Implementer brief

Setup and conventions for any subagent that implements or merges work in this repo. Prompts point here, so keep them to the task itself.

## Setup

- Integration branch: `csi-sensing`. A fresh worktree may start on the unrelated initial commit; create the work branch from `csi-sensing` (`git reset --hard csi-sensing` if the worktree has no work yet).
- Python: `/Users/andrewaburustum/Documents/codemetalforge/.venv/bin/python`. Run `python -m pytest` from the worktree root so the worktree's `csi` is the one imported. The sandbox refuses `PYTHONPATH`.
- Stay out of the main checkout's branch state: no `git switch` there, and leave `data/` alone.

## Work

- Build test-first with the `tdd` skill. Assert at the seam named in the ticket (simulated `(host_ts, line)` streams, or the dashboard's HTTP interface) and only on externally visible behaviour.
- Run the tests your change affects. The full suite is slow: start it in the background, wait for it to finish, and report only after it has.
- Leave hardware, serial ports, GitHub issues, `README.md` and pushing to the lead, unless the task says otherwise.
- A live dashboard may be running on port 8000 and holding serial ports. Use ephemeral ports in tests and leave that process running.

## Checks

- `scripts/check.sh` runs `ruff check .` and the fast test tier (`-m "not slow"`, parallel with `-n auto`). `scripts/check.sh --full` runs the whole suite before a merge.
- Enable the pre-push hook once per clone with `git config core.hooksPath .githooks`. It runs `scripts/check.sh`; skip it with `SKIP_CHECK=1`.

## Commits

End every commit message with:

```
Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
```

Merge `csi-sensing` into the work branch before reporting.

## Landing (merger)

Done means all of:

1. `csi-sensing` is merged into the work branch and `scripts/check.sh --full` passes there.
2. `csi-sensing` is fast-forwarded onto the work branch: `git -C <main checkout> merge --ff-only <work branch>`. This updates the branch without a `git switch`.
3. The ticket's issue is closed with a comment naming the branch and the merge commit. Issues do not auto-close, because GitHub only does that for the default branch (`main`).
4. The work branch and its worktree are removed once they are merged and clean.

## Handoff

Leave unfinished work findable: a comment on the ticket's issue saying what is left and which branch holds it. A "WIP" commit message alone is not a handoff.

## Report

Send one final report: branch, what changed, test results (counts), and what you could not verify. Send it when all background work has ended.
