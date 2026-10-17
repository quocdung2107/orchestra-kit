---
description: Orchestra — dispatch a ready task to the subagent matching its owner
argument-hint: [TASK-ID]
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Requested task (may be empty): $ARGUMENTS

Read only metadata; do not read code/plans. Every state write goes through `orc`.

1. If the session has no S-ID yet: open one with the command from the brief (`orc handoff open ...`), `--task` being the task about to run.
2. `orc next`. No task given → take the task on the `NEXT:` line. A given task must be ready.
   If there is a related `ASK USER` line → stop and ask the user.
3. `orc show <TASK-ID> --meta` to learn the owner.
   - owner `coder*` → context first (as in /orchestra:auto stage 2): `orc graph update` (skip on error) →
     `orc new context --task <TASK-ID> --summary "Context for <TASK-ID>"` → subagent **orchestra:context-builder**
     (`TASK: <TASK-ID>. Write the context into <CTX>. Return a YAML envelope.`) → `orc ctx seal <CTX>` →
     `orc set <TASK-ID> context=[<CTX>]`. An error at any step drops the CTX and continues; never stop because of the graph.
     Then subagent **orchestra:coder**, prompt: `TASK: <TASK-ID>. Return a YAML envelope.`
   - owner `researcher` → `orc new artifact --type plan --task <TASK-ID> [--supersedes <old plan ART in inputs>]`,
     then subagent **orchestra:researcher**, prompt: `TASK: <TASK-ID>. Write the plan into <ART-ID>. Return a YAML envelope.`
4. Before calling it: `orc advance <TASK-ID> --to dispatched`.
5. Load the envelope: `orc apply - <<'YAML'` … `YAML`. If orc refuses it (e.g. a file outside write_scope),
   report the error to the user verbatim; never repair state yourself.
6. Follow the `ROUTE:` line — `/orchestra:review <TASK-ID>` is the next step for code; a new plan goes to the user for approval as in /orchestra:plan steps 6–7.
