---
description: Orchestra for projects WITHOUT workflow K — 2 stages. (1) Understand & design, ask the user. (2) Plan → code → review → report
argument-hint: <request description>
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Request: $ARGUMENTS

Rules for A: read only metadata and `orc` command output; every state write goes through `orc`; pass IDs to subagents.
Do not report progress step by step; talk to the user only at stop points and in the final report.

## Stage 1 — Understand & design (always stops for the user)
1. If the session has no S-ID yet → run the `orc handoff open --key ...` command from the brief, with `--request "$ARGUMENTS"`.
2. `orc new task --title "<summary ≤80 chars>" --owner researcher --summary "<≤200>"` → ROOT.
   `orc set <S-ID> task=<ROOT>`. `orc new artifact --type research --task <ROOT> --summary "Understanding & design"` → UND.
3. `orc advance <ROOT> --to dispatched`; subagent **orchestra:researcher**:
   `TASK: <ROOT>. Write the understanding & design into <UND>. Return a YAML envelope.` → `orc apply - <<'YAML'` … `YAML`.
4. `orc ask <UND>` and send the user its output **verbatim**, ending with one line:
   "Answer the questions (or write 'agree with the recommendations'), and correct anything I misunderstood."
   If `orc ask` reports an error (unfilled section) → send the error to orchestra:researcher to fix, then rerun. **Stop and wait for the user.**
5. When the user answers:
   - For each point the user settles → `orc new decision --scope <UND> --summary "<≤200>"` (only a **proposal**: A may never
     accept a DEC itself; orc refuses `--status accepted`).
   - If the user corrects the understanding substantially or adds new requirements → `orc advance <ROOT> --to dispatched`, call orchestra:researcher again
     with the same ROOT/UND plus the user's points (1–3 sentences), `orc apply -`, then repeat step 4.
   - Clear enough → `orc approval request <UND> <the new DECs>`, send the user **verbatim** the lines it prints (the
     `orc:approve ...` line and the DEC list) and say "type the orc:approve line exactly to confirm". **Stop and wait.**
     Once the user has typed that line → `orc approve <UND> <the DECs>` (orc accepts only when it sees exactly the line the user typed; UND + DECs become
     accepted) → stage 2. Never type or copy the `orc:approve` line on the user's behalf.

## Stage 2 — Plan → code → review → report (runs on its own)
1. `orc new task --title "Plan: <summary>" --owner researcher --inputs <UND> <the new DECs>` → PT;
   `orc new artifact --type plan --task <PT> --depends-on <UND>` → PLAN.
   `orc next`, `orc advance <PT> --to dispatched`; orchestra:researcher: `TASK: <PT>. Write the plan into <PLAN>. Return a YAML envelope.` → `orc apply -`.
2. `orc gate <PLAN>`: `PROCEED` → `orc advance <PLAN> --to accepted`, `orc new task --from <PLAN>`.
   `ASK` → **stop point** (report the lines after `ASK`).
3. Repeat until `orc next` shows no `READY:`/`REVIEW:` for this request:
   - `ASK USER` → **stop point**.
   - `REVIEW: <T>` → `orc verify <T>` (creates EVD `by: orc`; exit 6 FAIL / 7 INCOMPLETE environment: still pass it on to orchestra:reviewer
     and note it in the report; the error "no checkpoint" because the project has no `verify:` section ⇒ skip this step; any other error = stop point) → `orc graph update` (skip on error) → `orc new artifact --type review_report --task <T>` → R;
     orchestra:reviewer: `TASK: <T>. Report into <R>. Use orc show <T> --pack. Return a YAML envelope.` → `orc apply -`.
   - `READY: <T>` with owner coder → **context first**:
     1. `orc graph update` — optional; on error/not configured (exit code 4) skip it, **never stop because of the graph**.
     2. `orc new context --task <T> --summary "Context for <T>"` → CTX; subagent **orchestra:context-builder**:
        `TASK: <T>. Write the context into <CTX>. Return a YAML envelope.`
     3. `orc ctx seal <CTX>`; on error → send it verbatim to orchestra:context-builder to fix (at most 2 times); if it still fails, drop the CTX and continue.
     4. Sealed → `orc set <T> context=[<CTX>]`.
     Then `orc advance <T> --to dispatched`; orchestra:coder: `TASK: <T>. Return a YAML envelope.` → `orc apply -`.
   - `READY: <T>` with owner researcher (replan) → `orc advance <T> --to dispatched`,
     `orc new artifact --type plan --task <T> --supersedes <old plan>`, then orchestra:researcher → `orc apply -`.
   - `orc` rejects an envelope → send the error verbatim to that subagent to fix, at most 2 times, then **stop point**.
4. Finish:
   - Code changes live on the branch `orchestra/<ROOT>` (in the code repo, if it is separate), committed; **never merge or push**. Commit `.orchestra/` in the project repo.
   - Fill in `.orchestra/sessions/<S-ID>.md`, `orc handoff check --key ...`, `orc validate`.
   - Reply to the user **once**: `orc report <PT>` (keep the table as is) + at most 5 lines: assumptions you chose,
     what the user should test by hand, branches to merge.

## Stop points in stage 2 (only these)
`orc gate` = ASK · `ASK USER` (DECISION_CONFLICT, attempts > 2, blocked, failed) · a REGRESSION not fixed
after 1 round · an environment error you cannot fix. When stopping: handoff `result: blocked`, ask one question answerable in
one word, with a recommended option.

To cancel an unfinished task (duplicate, skipped): the user types a separate line `orc:abort <TASK>` → `orc abort <TASK>` (the task becomes
`aborted` and leaves the brief). Never type an `orc:` line on the user's behalf.
