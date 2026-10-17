---
description: Orchestra — plan a request with the orchestra:researcher subagent
argument-hint: <request description>
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Request: $ARGUMENTS

Rules for A: read only metadata (`orc show <ID> --meta`, `orc next`, the brief); do not read
plan/code content; every state write goes through `orc`. Pass IDs to subagents, not long chat.

1. If the session has no S-ID yet: run `orc handoff open --key ... --role orchestrator --request "..."`
   exactly as in the brief at the start of the session. Remember the S-ID.
2. `orc new task --title "<request summary ≤80 chars>" --owner researcher --summary "<≤200 chars>"` → TASK-ID.
   Then `orc set <S-ID> task=<TASK-ID>`.
3. `orc new artifact --type plan --task <TASK-ID> --summary "<≤200 chars>"` → ART-ID.
4. `orc advance <TASK-ID> --to dispatched`, then call subagent **orchestra:researcher** with a prompt of only:
   `TASK: <TASK-ID>. Write the plan into <ART-ID>. Return a YAML envelope.`
5. Load the YAML envelope the subagent returns into state as is:
   `orc apply - <<'YAML'` … `YAML`. Read the `ROUTE:` line.
6. Tell the user: the path `.orchestra/artifacts/<ART-ID>.md`, the summary (`orc show <ART-ID> --meta`),
   the decisions awaiting approval. Run `orc approval request <ART-ID> <the plan's proposed DECs>` and send the user the
   `orc:approve ...` line it prints, verbatim. **Stop and wait.**
7. Once the user has typed exactly that line: `orc approve <ART-ID> <the DECs>` (plan + DECs become accepted),
   `orc new task --from <ART-ID>`, `orc next`.
   If the user asks for changes → call orchestra:researcher again with the same TASK/ART plus the user's points (1–2 sentences).
