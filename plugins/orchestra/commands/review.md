---
description: Orchestra — review an in_review task with the orchestra:reviewer subagent, routed by failure_class
argument-hint: <TASK-ID>
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Task: $ARGUMENTS

1. `orc show <TASK-ID> --meta`: the status must be `in_review`.
2. `orc verify <TASK-ID>`: orc checks the scope with git and runs the check commands itself, creating EVD `by: orc` (exit 6 = fail: still review,
   and report the `MISMATCH`/outside-write_scope lines to the user). If the project has no `verify:` section or the task has no checkpoint → skip.
   `orc new artifact --type review_report --task <TASK-ID>` → ART-ID.
3. Call subagent **orchestra:reviewer** with a prompt of only:
   `TASK: <TASK-ID>. Report into <ART-ID>. Use orc show <TASK-ID> --pack. Return a YAML envelope.`
   Never pass the coder's summary or reasoning to orchestra:reviewer.
4. `orc apply - <<'YAML'` … `YAML`. Routing is done by orc; A only follows the `ROUTE:` line:
   - `→ done`: tell the user it is done, `orc next`.
   - `send ... back to coder` (IMPL_BUG): `/orchestra:run <TASK-ID>`.
   - `researcher replan` (SPEC_GAP): `/orchestra:run <new TASK>` (the new plan supersedes the old one), then take it to the user for approval.
   - `BLOCK MERGE` (REGRESSION): do not commit/merge the task's changes; `/orchestra:run <fix task>`.
   - `ASK USER` (DECISION_CONFLICT or attempts > 2): stop and ask the user with the summary + ID.
