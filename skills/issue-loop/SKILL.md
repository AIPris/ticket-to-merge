---
name: issue-loop
description: Autonomously work through all open GitHub issues of the current repo - a fresh Claude session per issue, an independent code review, then commit, PR, CI wait/repair, squash merge. Only run when the user explicitly invokes /issue-loop.
disable-model-invocation: true
argument-hint: "[--check | --max-issues N]"
---

# Issue loop (global, Windows)

Runs `${CLAUDE_SKILL_DIR}/run_issues.py` against the git repository in the
current working directory. The script is a Python supervisor: for each open issue (oldest first) it starts a
**fresh** `claude -p` worker, verifies locally, commits, has the commits reviewed, pushes `agent/issue-N`,
opens a PR (`Closes #N`), waits for CI (fresh repair sessions on red CI), squash-merges, deletes the branch,
then continues. It stops on any other failure and never bypasses branch protection.

**Code review gate** (before every push, including pushes of CI repairs): a fresh read-only reviewer session
checks the commits. It blocks only on bugs, unsolved issue requirements, missing tests and broken
CLAUDE.md/AGENTS.md rules. On findings, a fresh fixer session fixes or rebuts each one, then a narrow
re-review follows (default: up to 2 fix rounds). Each approval is posted on the PR as a comment. If the last
re-review still requests changes, the issue is **parked**, not merged: branch and PR stay open with the
findings as a PR comment, the issue gets the label `agent-needs-human`, and the loop moves on to the next issue.
Parked issues are skipped until someone removes the label.

**Usage limit:** if a Claude session hits the usage limit (5-hour or weekly), the supervisor does not stop.
It sleeps until the reset time Claude reports (plus 2 minutes), then re-runs that session fresh; a writing
session is told that the tree may hold the interrupted attempt's partial work. `--status` shows the resume
time. A weekly limit can mean a wait of days. Other API errors (overloaded, 5xx) still stop the loop.

## Procedure

1. Run the read-only preflight and show the user the output (repo, base branch, verify commands, next issue):

   ```
   python "${CLAUDE_SKILL_DIR}/run_issues.py" --repo . --check
   ```

   If it fails (dirty tree, unpushed commits on the base branch, wrong branch, missing tool, no auth), explain
   the cause and stop. Do not "fix" the user's working tree yourself.

2. State clearly: the workers run with `--dangerously-skip-permissions`, and merges into the default branch
   happen automatically. Ask the user to confirm before starting (use the scope in `$ARGUMENTS`, e.g.
   `--max-issues 1`; if none was given, run without a limit, i.e. until all open issues are solved. Say so
   in the confirmation; do not suggest or add `--max-issues` on your own).

3. After confirmation start it in the background so the session stays usable:

   ```
   python "${CLAUDE_SKILL_DIR}/run_issues.py" --repo . $ARGUMENTS
   ```

   Use `run_in_background`. To report progress, run the quick status check (read-only, safe while the loop runs):

   ```
   python "${CLAUDE_SKILL_DIR}/run_issues.py" --repo . --status
   ```

   It prints RUNNING / not running, the current issue, the stage (Claude working, verifying, waiting for CI,
   merging, ...), how many issues were merged this run, and seconds since the last activity (flags >10 min as
   possibly stuck). Prefer this over reading the raw output file. State lives in
   `%LOCALAPPDATA%\issue-loop\<repo>.status.json`. While it runs, do **not** touch the
   repository (no edits, checkouts or commits) - the supervisor needs a clean tree.

4. When it finishes, summarize: issues merged, issues parked (with their PR links), or the exact stop reason
   and where unfinished work was kept (`agent/failed-issue-N-*` branch, or an open PR).

## Per-repo settings

Optional `<repo>\.issue-loop.json` (commit it - the tree must stay clean):

```json
{ "verify": ["npm run build", "npm test"], "protected_paths": ["vendor/"], "model": "opus",
  "worker_max_turns": 200, "ci_repair_max_turns": 100, "ci_repair_attempts": 3, "local_repair_attempts": 2,
  "review": true, "review_rounds": 2, "review_max_turns": 80 }
```

`"review": false` turns the review gate off. `review_rounds` is the number of fix + re-review rounds before
an issue is parked.

Without `verify`, commands are auto-detected (package.json `build`/`test` scripts, or pytest).
Logs and lock files live in `%LOCALAPPDATA%\issue-loop\`.
