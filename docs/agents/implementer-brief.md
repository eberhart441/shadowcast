# Implementer brief

Setup and conventions for any subagent that implements or merges work in this repo. Prompts point here, so keep them to the task itself.

## Branch flow

Work moves in three steps: a **work branch**, then the **integration branch** named in the task, then a pull request to `main`.

- A work branch is cut from the integration branch the task names, or from `main` when the task names none, and is named `ticket/<issue>-<slug>`.
- A merger lands finished work branches on the integration branch.
- The integration branch reaches `main` through one pull request (see "Landing on main").
- The task names the issue to read and the spec it belongs to. Spec #2 (the scaling redo) has its research and plan in `docs/research/scaling-redo/`, starting at `00-synthesis.md`.

## Setup

- Make a worktree for the work branch: `git worktree add <short path> -b ticket/<issue>-<slug> <integration branch>`. Confirm with `git -C <worktree> merge-base --is-ancestor <integration branch> HEAD`; if that fails and you have no work yet, `git reset --hard <integration branch>`. Keep the path short: the Windows 260-character limit matters for ESP-IDF builds.
- Python: an existing virtualenv's interpreter, by absolute path, since a new worktree has no `.venv` (`docs/agents/project-context.md`, "What bites"). Run `python -m pytest` from the worktree root so the worktree's `csi` is the one imported. The sandbox refuses `PYTHONPATH`.
- Stay out of the main checkout's branch state: no `git switch` there, and leave `data/` alone.

## Work

- Build test-first with the `tdd` skill. Assert at the seam named in the ticket (simulated `(host_ts, line)` streams, or the dashboard's HTTP interface) and only on externally visible behaviour. A docs-only ticket has no behaviour to test; the docs lint (`tests/test_docs.py`) is its check.
- Use the words in `GLOSSARY.md`. Add a term there when the ticket introduces one, and write an ADR when the ticket says so or you make a hard-to-reverse decision.
- Run the tests your change affects. The full suite is slow: start it in the background, wait for it to finish, and report only after it has.
- Leave hardware, serial ports, GitHub issues, `README.md` and pushing to the lead, unless the task says otherwise. A rig check is for the user: write the procedure and leave its checkbox open.
- A live dashboard may be running on port 8000 and holding serial ports. Use ephemeral ports in tests and leave that process running.

## Checks

- `scripts/check.sh` runs `ruff check .` and the fast test tier (`-m "not slow"`, parallel with `-n auto`). `scripts/check.sh --full` runs the whole suite before a merge.
- Enable the pre-push hook once per clone with `git config core.hooksPath .githooks`. It runs `scripts/check.sh`; skip it with `SKIP_CHECK=1`.

## Commits

End every commit message with:

```
Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
```

Merge the integration branch (or `main`, if the work branch was cut from it) into the work branch before reporting.

## Landing (merger)

Lands a work branch on the integration branch. Done means all of:

1. The integration branch is merged into the work branch and `scripts/check.sh --full` passes there.
2. The integration branch is fast-forwarded onto the work branch from its own worktree, or with `git -C <checkout> merge --ff-only <work branch>`. Never `git switch` in the main checkout.
3. The ticket's issue is closed with a comment naming the branch and the merge commit. Issues do not auto-close, because GitHub only does that for the default branch (`main`).
4. The work branch and its worktree are removed once they are merged and clean.

## Handoff

Leave unfinished work findable: a comment on the ticket's issue saying what is left and which branch holds it. A "WIP" commit message alone is not a handoff.

## Report

Send one final report: branch, what changed, test results (counts), and what you could not verify. Send it when all background work has ended.

## Landing on main

Takes the integration branch to `main`, after the steps above.

- Work reaches `main` through a pull request from the integration branch.
- Run `git fetch` before comparing with `origin/main`. A PR merged on GitHub leaves the local `origin/main` stale.
- Wait for CI with `gh pr checks <n> --watch`.
- A CI failure that passes on `gh run rerun <id> --failed` is a flake. Report it; do not ignore it.
- Merge with `gh pr merge <n> --merge`. A merge commit keeps the history.
- Commits made after a merge need a new PR.
