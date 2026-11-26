---
description: Orcgate — review an in_review task with the orcgate:reviewer subagent, routed by failure_class
argument-hint: <TASK-ID>
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Task: $ARGUMENTS

**Models.** Before every subagent call run `orc model <TASK-ID>` (add `--role context-builder` or `--role reviewer` for those agents) and pass the printed alias as the Agent tool's `model` parameter; on the matching `orc advance <TASK-ID> --to dispatched` add `--model <same alias>`. `orc next` shows the same `model=` on READY/REVIEW lines. A coder task that was sent back (attempts ≥ 1) gets the stronger model automatically. Never choose a model yourself.
**Foreground subagents.** Call every subagent with the Agent tool's `run_in_background: false` and wait for its envelope in the same turn; the steps are sequential, so nothing should run in the background. To have a subagent fix something, make a new foreground Agent call (same TASK, plus the error verbatim) instead of SendMessage. Never end a turn just to wait for a background notification; if a subagent did start in the background, read its result with the task output tool before going on. Run `orc verify` in the foreground with the Bash tool's maximum timeout.

1. `orc show <TASK-ID> --meta`: the status must be `in_review`.
2. `orc verify <TASK-ID>`: orc checks the scope with git and runs the check commands itself, creating EVD `by: orc` (exit 6 = fail: still review,
   and report the `MISMATCH`/outside-write_scope lines to the user). If the project has no `verify:` section or the task has no checkpoint → skip.
   `orc new artifact --type review_report --task <TASK-ID>` → ART-ID.
3. Call subagent **orcgate:reviewer** with a prompt of only:
   `TASK: <TASK-ID>. Report into <ART-ID>. Use orc show <TASK-ID> --pack. Return a YAML envelope.`
   Never pass the coder's summary or reasoning to orcgate:reviewer.
4. `orc apply - <<'YAML'` … `YAML`. Routing is done by orc; A only follows the `ROUTE:` line:
   - `→ done`: tell the user it is done, `orc next`.
   - `send ... back to coder` (IMPL_BUG): `/orcgate:run <TASK-ID>`.
   - `researcher replan` (SPEC_GAP): `/orcgate:run <new TASK>` (the new plan supersedes the old one), then take it to the user for approval.
   - `BLOCK MERGE` (REGRESSION): do not commit/merge the task's changes; `/orcgate:run <fix task>`.
   - `ASK USER` (DECISION_CONFLICT or attempts > 2): stop and ask the user with the summary + ID.
