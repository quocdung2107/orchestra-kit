---
description: Orcgate — dispatch a ready task to the subagent matching its owner
argument-hint: [TASK-ID]
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Requested task (may be empty): $ARGUMENTS

Read only metadata; do not read code/plans. Every state write goes through `orc`.

**Models.** Before every subagent call run `orc model <TASK-ID>` (add `--role context-builder` or `--role reviewer` for those agents) and pass the printed alias as the Agent tool's `model` parameter; on the matching `orc advance <TASK-ID> --to dispatched` add `--model <same alias>`. `orc next` shows the same `model=` on READY/REVIEW lines. A coder task that was sent back (attempts ≥ 1) gets the stronger model automatically. Never choose a model yourself.
**Foreground subagents.** Call every subagent with the Agent tool's `run_in_background: false` and wait for its envelope in the same turn; the steps are sequential, so nothing should run in the background. To have a subagent fix something, make a new foreground Agent call (same TASK, plus the error verbatim) instead of SendMessage. Never end a turn just to wait for a background notification; if a subagent did start in the background, read its result with the task output tool before going on. Run `orc verify` in the foreground with the Bash tool's maximum timeout.

1. If the session has no S-ID yet: open one with the command from the brief (`orc handoff open ...`), `--task` being the task about to run.
2. `orc next`. No task given → take the task on the `NEXT:` line. A given task must be ready.
   If there is a related `ASK USER` line → stop and ask the user.
3. `orc show <TASK-ID> --meta` to learn the owner.
   - owner `coder*` → context first (as in /orcgate:auto stage 2): `orc graph update` (skip on error) →
     `orc new context --task <TASK-ID> --summary "Context for <TASK-ID>"` → subagent **orcgate:context-builder**
     (`TASK: <TASK-ID>. Write the context into <CTX>. Return a YAML envelope.`) → `orc ctx seal <CTX>` →
     `orc set <TASK-ID> context=[<CTX>]`. An error at any step drops the CTX and continues; never stop because of the graph.
     Then subagent **orcgate:coder**, prompt: `TASK: <TASK-ID>. Return a YAML envelope.`
   - owner `researcher` → `orc new artifact --type plan --task <TASK-ID> [--supersedes <old plan ART in inputs>]`,
     then subagent **orcgate:researcher**, prompt: `TASK: <TASK-ID>. Write the plan into <ART-ID>. Return a YAML envelope.`
4. Before calling it: `orc advance <TASK-ID> --to dispatched`.
5. Load the envelope: `orc apply - <<'YAML'` … `YAML`. If orc refuses it (e.g. a file outside write_scope),
   report the error to the user verbatim; never repair state yourself.
6. Follow the `ROUTE:` line — `/orcgate:review <TASK-ID>` is the next step for code; a new plan goes to the user for approval as in /orcgate:plan steps 6–7.
