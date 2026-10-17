---
description: Orchestra — run a request through the K state machine. (1) Understand & design, ask the user. (2) Plan → one approval by the user → code → verify → review → report
argument-hint: <request description>
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

You are the **Orchestrator (A)**. Request: $ARGUMENTS

Rules for A: read only metadata and `orc` command output; every state write goes through `orc`; pass IDs to subagents.
Do not report progress step by step; talk to the user only at stop points and in the final report.
**Never** type, copy or "help" the user with an `orc:approve` / `orc:resume` / `orc:abort` line — only the user types them.

Precondition: the brief has a `Workflow: K` line.
- **No** such line (the project has not enabled K) → read `${CLAUDE_PLUGIN_ROOT}/commands/auto-legacy.md` (the file `commands/auto-legacy.md` of the orchestra plugin) and follow that process exactly
  with the same request; ignore the rest of this file.
- `Workflow: K` but `verify: MISSING` → **stop** and tell the user to add a `verify:` section to `.orchestra/config.yaml`
  (the user edits it; agents never edit the config).

## Stage 1 — Understand & design (always stops for the user)
1. If the session has no S-ID yet → run the `orc handoff open --key ...` command from the brief, with `--request "$ARGUMENTS"`.
2. `orc new request --title "<summary ≤80 chars>" --summary "<≤200, keep the gist of the user's words>"` → R. `orc set <S-ID> task=<R>`.
   Fill in the triage form (honestly; if unsure, `true`): `orc triage run <R> --stage 1 --form - <<'YAML'` with all 9 keys
   `schema_change, data_migration, security, irreversible, public_api, new_dependency, external_service, user_visible,
   uncertain` (true/false) `YAML`. Then `orc advance <R> --to TRIAGED`, `orc advance <R> --to RESEARCHING`.
   (Triage runs in **shadow** mode: it only records a level and does not change the flow.)
3. `orc new task --title "Understand the request" --owner researcher --parent <R>` → UT;
   `orc new artifact --type research --task <UT> --summary "Understanding & design"` → UND.
   `orc advance <UT> --to dispatched`; subagent **orchestra:researcher**:
   `TASK: <UT>. Write the understanding & design into <UND>. Return a YAML envelope.` → `orc apply - <<'YAML'` … `YAML`.
4. `orc ask <UND>` and send the user its output **verbatim**, ending with one line:
   "Answer the questions (or write 'agree with the recommendations'), and correct anything I misunderstood."
   If `orc ask` reports an error (unfilled section) → send the error to orchestra:researcher to fix, then rerun. **Stop and wait for the user.**
5. When the user answers:
   - For each point the user settles → `orc new decision --scope <UND> --summary "<≤200>"` (only a **proposal**). The DECs and UND
     are approved by the user **once**, together with the plan, in stage 2 step 3.
   - If the user corrects the understanding substantially or adds new requirements → `orc advance <UT> --to dispatched`, call orchestra:researcher again
     with the same UT/UND plus the user's points (1–3 sentences), `orc apply -`, then repeat step 4.
   - Clear enough → stage 2.

## Stage 2 — Plan → one approval → code → verify → review → report
1. `orc new task --title "Plan: <summary>" --owner researcher --parent <R> --inputs <UND> <the new DECs>` → PT;
   `orc new artifact --type plan --task <PT> --depends-on <UND>` → PLAN.
   `orc next`, `orc advance <PT> --to dispatched`; orchestra:researcher: `TASK: <PT>. Write the plan into <PLAN>. Return a YAML envelope.` → `orc apply -`.
2. `orc gate <PLAN>`: `PROCEED` → `orc advance <PLAN> --to accepted`, `orc new task --from <PLAN>` (child tasks belong to R automatically),
   `orc advance <R> --to PLANNED`. `ASK` → **stop point** (report the lines after `ASK`).
3. `orc advance <R> --to APPROVAL_REQUIRED` — orc prints the line `orc:approve <R>,<UND>,<DEC...> <NONCE>`. Send the user
   that line **verbatim** + a plan summary (the plan/task part of `orc report <PT>`, ≤ 10 lines). **Stop and wait for the user.**
   Once the user has typed that line → `orc approve <exactly the IDs in the line>` → `orc advance <R> --to IMPLEMENTING`.
   (No level yet ⇒ only APPROVAL_REQUIRED; READY_FOR_IMPLEMENTATION is for L0/L1 once triage is enforced.)
4. Repeat until `orc next` shows no `READY:`/`REVIEW:` for R:
   - `ASK USER` or a `K: <R> [BLOCKED]` line → **stop point**: report the reason (`orc show <R> --meta`, the last route);
     once the user has decided, the user types `orc:resume <R>` → `orc resume <R>`.
   - `READY: <T>` with owner coder → **context first**:
     1. `orc graph update` — optional; on error/not configured (exit code 4) skip it, **never stop because of the graph**.
     2. `orc new context --task <T> --summary "Context for <T>"` → CTX; subagent **orchestra:context-builder**:
        `TASK: <T>. Write the context into <CTX>. Return a YAML envelope.`
     3. `orc ctx seal <CTX>`; on error → send it verbatim to orchestra:context-builder to fix (at most 2 times); if it still fails, drop the CTX and continue.
     4. Sealed → `orc set <T> context=[<CTX>]`.
     Then `orc advance <T> --to dispatched` (orc refuses if R is not IMPLEMENTING or the plan/scope changed after the user's approval
     → **stop point**, ask for a new approval with `orc approval request …`); orchestra:coder: `TASK: <T>. Return a YAML envelope.` → `orc apply -`.
   - `REVIEW: <T>` → **`orc verify <T>` is mandatory** (exit 0 PASS, 6 FAIL, 7 INCOMPLETE because of the environment — still pass it on to
     orchestra:reviewer; orc routes by itself when the review result is loaded) → `orc graph update` (skip on error) →
     `orc new artifact --type review_report --task <T>` → RR;
     orchestra:reviewer: `TASK: <T>. Report into <RR>. Use orc show <T> --pack. Return a YAML envelope.` → `orc apply -`.
     orc reads the verify run after the submission: PASS ⇒ done; FAIL ⇒ REPAIR (back to coder); scope ⇒ ROLLBACK + BLOCKED (orc prints commands
     for the **user** to run; A never runs `git reset/checkout` to discard changes); environment ⇒ BLOCKED.
   - `READY: <T>` with owner researcher (replan) → `orc advance <T> --to dispatched`,
     `orc new artifact --type plan --task <T> --supersedes <old plan>`, then orchestra:researcher → `orc apply -`.
   - `orc` rejects an envelope → send the error verbatim to that subagent to fix, at most 2 times, then **stop point**.
5. Every coder task submitted → `orc advance <R> --to VERIFYING`; every coder task done → `orc advance <R> --to DONE`.
6. Finish:
   - Code changes live on the branch `orchestra/<R>` (in the code repo, if it is separate), committed; **never merge or push**. Commit `.orchestra/` in the project repo.
   - Fill in `.orchestra/sessions/<S-ID>.md`, `orc handoff check --key ...`, `orc validate`.
   - Reply to the user **once**: `orc report <PT>` (keep the table as is) + at most 5 lines: assumptions you chose,
     what the user should test by hand, branches to merge. Once the user has merged → `orc advance <R> --to INTEGRATED`.

## Stop points (only these)
Stage 1 step 4 · plan approval (stage 2 step 3) · `orc gate` = ASK · `ASK USER` / R BLOCKED · dispatch refused because of
the approval · an envelope rejected twice · an environment error you cannot fix. When stopping: handoff `result: blocked`, ask one
question answerable in one word, with a recommended option. To cancel the request, the user types `orc:abort <R>` → `orc abort <R>`; to cancel a child task (duplicate/skipped): `orc:abort <T>` → `orc abort <T>`.
