# SCHEMA — orchestra-kit 0.2.0

A blackboard-style multi-agent system for Claude Code: state lives in files (.md/.yaml + git), and agents
pass IDs to each other instead of long chat.

## 1. Roles

| Role | Implementation | Reads | Writes |
|---|---|---|---|
| A Orchestrator | the main Claude Code session + `/orchestra:auto` (full loop) or `/orchestra:plan` `/orchestra:run` `/orchestra:review` `/orchestra:next` `/orchestra:handoff` | brief, `orc show --meta`, `orc next` (metadata only) | only through `orc` |
| B Researcher | subagent `orchestra:researcher` | task, earlier plans, code, docs | the body of the issued ART file |
| C Coder | subagent `orchestra:coder` (owner `coder`, `coder-1`, …) | task + acceptance, context | files in `write_scope` |
| D Reviewer | subagent `orchestra:reviewer` | `orc show TASK --pack` (acceptance + outputs + evidence) | the body of the review_report ART file |
| Context Builder | subagent `orchestra:context-builder` | task, plan, KNOWLEDGE_MAP, `orc graph ...` | the body of the issued CTX file |

Single writer: only `orc` writes `.orchestra/state/`. Writing commands hold the lock `runtime/state.lock` and write files
atomically (tmp + rename); every `status` change is appended to `state/journal.jsonl` (a hash chain checked by `orc validate`).
B/C/D return a **YAML envelope**; A loads it with `orc apply -`.

## 2. IDs

`TASK-0001`, `ART-0001`, `DEC-0001`, `CTX-0001`, `EVD-0001`, `S-0001`. Numbered per project
(highest existing + 1). `project_id` lives in `.orchestra/config.yaml`.
Acceptance criteria are referenced as `ART-0001#AC-2`, pointing at the line `- AC-2: ...` of a plan.

## 3. Layout

```
orchestra-kit/                          (this repository = a plugin marketplace)
  .claude-plugin/marketplace.json
  plugins/orchestra/
    .claude-plugin/plugin.json
    agents/{researcher,coder,reviewer,context-builder}.md
    commands/{auto,auto-legacy,plan,run,review,next,handoff}.md
    hooks/hooks.json  hooks/{session_start,stop_check,prompt_log,guard}
    bin/orc  bin/orc.cmd                (added to PATH by Claude Code while the plugin is enabled)
    cli/orc_core.py …  templates/  VERSION
  tests/  tools/release_check.py  SCHEMA.md  README.md  CHANGELOG.md  LICENSE

<project>/
  CLAUDE.md
  .orchestra/
    config.yaml            project_id, kit_version, goal (+ workflow, verify, triage, graph, guard)
    state/{tasks,artifacts,decisions,contexts,evidence}.yaml   (registries, written only by orc)
    state/journal.jsonl  state/approvals.yaml
    artifacts/ART-x.md     frontmatter + content
    decisions/DEC-x.md     the full text of a decision
    contexts/CTX-x.md  evidence/EVD-x.md  sessions/S-x.md
    knowledge/KNOWLEDGE_MAP.md
    runtime/               session markers, prompt log, guard log (gitignored)
    cache/                 graph cache (gitignored)
```

## 4. Registries (`state/*.yaml`, each file is a list)

**Task**: `id, title, status, owner, blocked_by[], context[], inputs[], outputs[], acceptance[], write_scope[], attempts, summary, failure_class, needs_user, resume_status, level, risk_flags[], parent, workflow, recovery_action`
- status: `pending|ready|dispatched|in_review|done|failed|blocked|aborted` (K root tasks use the K states, §6c)
- owner: `orchestrator|researcher|reviewer|coder|coder-N`
- `needs_user`, `resume_status`: the "ask the user" flag and the status to return to once unblocked.

**Artifact** (the registry entry = the file's frontmatter; every field must match):
`id, type(plan|research|code_patch|review_report), task, version, supersedes, status(draft|accepted|rejected), summary, depends_on[], paths[]`

**Decision**: `id, status(proposed|accepted|superseded), scope, summary, supersedes`.
Accepting a DEC with `supersedes` moves the old DEC to `superseded` automatically.

**Context**: `id, task, summary, paths[], source, graph_state, fingerprint, truncated` · **Evidence**: `id, task, kind(test|lint|build|manual|other|typecheck|scope), result(pass|fail|info), summary, paths[], by(agent|orc)` — `typecheck|scope` and `by: orc` are created only by `orc verify`.

`summary` is ≤ 200 characters everywhere.

## 5. Envelope (subagent → A)

YAML only (a ```` ```yaml ```` fence is allowed), no unknown keys:

```yaml
msg: result
task_id: TASK-0003
status: done            # done | partial | blocked | failed
outputs: []             # IDs, or paths relative to the project root
evidence: []            # EVD IDs, or {kind, result, summary, paths} → orc creates the EVD
proposed_decisions: []  # strings, or {summary, scope, supersedes} → orc creates a proposed DEC
summary: "≤200"
failure_class: null     # reviewer only
```

## 6. Deterministic routing (`orc apply`)

| When received | Result | Task becomes |
|---|---|---|
| dispatched, owner coder | done | `in_review` → /orchestra:review |
| dispatched, other owner | done | `done` |
| dispatched | partial | `ready` |
| dispatched | blocked / failed | `blocked` / `failed` + needs_user |
| in_review | done, no failure_class | `done` |
| in_review | IMPL_BUG | attempts+1 → `ready` (back to coder); attempts > 2 → `blocked` + needs_user |
| in_review | SPEC_GAP | creates a researcher task "Replan" (the new plan uses `--supersedes`); the task becomes `blocked`, resumes as `ready` |
| in_review | DECISION_CONFLICT | `blocked` + needs_user |
| in_review | REGRESSION | merge blocked; creates a coder fix task; the task becomes `blocked`, resumes as `in_review` |

A coder returning a file outside `write_scope` → orc rejects the whole envelope and writes nothing.
`orc next`: `pending` with all blocked_by `done` → `ready`; `blocked` (without needs_user) once unblocked → `resume_status`.

## 6b. Two-stage workflow (projects without `workflow: k`)

`/orchestra:auto` falls back to `commands/auto-legacy.md` when the brief has no `Workflow: K` line.

1. **Understand & design — always stops.** The researcher writes a `research` ART with 4 sections: My understanding of the request ·
   Preliminary design · Unclear / missing information · Questions for the user. A sends the user `orc ask ART-x` verbatim.
   The user's answers → **proposed** DECs (scope = the research ART); A runs `orc approval request <ART> <DEC...>`,
   the user types the `orc:approve ...` line, and `orc approve` moves the research ART + DECs to accepted.
2. **Plan → code → review → report — runs on its own.** The plan task's inputs are the research ART + DECs; `orc gate` →
   the plan is accepted → the coder/reviewer loop follows `orc next` + `ROUTE:` → commit on the branch `orchestra/<ROOT>`
   (no merge/push) → handoff → reply with `orc report <plan task>`. It stops only on: `orc gate` = ASK, `ASK USER`,
   an envelope rejected twice, an environment error.

`orc ask ART-x` refuses if the ART is not a research artifact or still has unfilled sections.

## 6c. State machine K (opt-in: `workflow: k` in the config)

**Root task** (`orc new request`, owner orchestrator, `workflow: k`):
`NEW → TRIAGED → RESEARCHING → PLANNED → READY_FOR_IMPLEMENTATION (L0/L1) | APPROVAL_REQUIRED (L2/L3, or no
level) → IMPLEMENTING → VERIFYING → DONE → INTEGRATED`; side states `BLOCKED · FAILED · ABORTED`. Changed only with `orc advance`
(orc checks the conditions: research with all 4 sections + accepted plan + coder tasks ⇒ PLANNED; a valid approval ⇒
IMPLEMENTING; every coder task submitted ⇒ VERIFYING; every coder task done ⇒ DONE). Entering APPROVAL_REQUIRED/READY, orc requests
**one** approval covering the root + the research ART + proposed DECs. The root's binding: accepted plans of the child tasks, the combined write_scope,
the list of coder tasks, level, risk_flags.
**Child tasks** (`parent` = the root; required in K projects): the short loop of §6, but a coder task is `dispatched` only while the root is IMPLEMENTING
and the approval is still valid; when the reviewer passes it, orc reads the `orc verify` run **after the submission** (journal `event: verify`):
PASS ⇒ done; FAIL ⇒ REPAIR; scope ⇒ ROLLBACK (commands printed for the user) + ESCALATE; environment ⇒ ESCALATE; no verify yet ⇒
the envelope is refused. A failed review follows table I2 (`orc_k.recovery`). ESCALATE ⇒ the root becomes BLOCKED.
`orc resume R` / `orc abort R` only after the user typed `orc:resume R` / `orc:abort R`. `orc abort` also cancels ordinary tasks (→ `aborted`).
A K project must have a `verify:` section; without it the brief shows `verify: MISSING` and K cannot reach DONE.

Triage (**shadow**): `orc triage run R --stage 1|2|3 [--form F]` / `show` / `downgrade --to N`
(needs the user to type `orc:downgrade R L<n> <reason>`). Runs automatically when a K root enters TRIAGED (form), PLANNED (write_scope) and VERIFYING
(diff). Level = max of the rules (L3 risk_paths, form, files/lines/modules, risk keywords; thresholds in `config.triage`); it only goes up; missing facts ⇒
at least L1. Shadow writes journal `event: triage` and does not write `task.level`.

## 7. Sessions and handoff

- The **SessionStart** hook loads `orc brief` (≤ 6000 characters ≈ 1.5k tokens) with the Claude session key (`session_id`).
- When a session takes a request that creates or works on tasks: `orc handoff open --key <session_id> --role ... --task ... --request "..."`
  → creates `sessions/S-x.md` (result `pending`) + a marker in `runtime/`.
- The **Stop** hook blocks only when the session opened an S-ID and the handoff is not valid. It blocks at most 3 times,
  then lets the session stop (prevents endless loops).
- A valid handoff: frontmatter `id, task, role, result(done|partial|blocked)` + all 5 sections
  `## Done`, `## Files changed`, `## Decisions made`, `## Open questions`, `## Next steps`
  (write "None" in an empty section).
- Every hook is a no-op when no `.orchestra/config.yaml` exists in cwd or a parent directory, and never breaks
  the session when the kit fails (exit 0).

## 8. `orc` commands

| Command | Does |
|---|---|
| `orc init --project-id X --goal "..."` | creates `.orchestra/`; creates CLAUDE.md only if there is none |
| `orc brief [--key K]` | brief of at most ~1.5k tokens |
| `orc new request|task|artifact|decision|context|evidence ...` | creates an entry, prints its ID; `orc new task --from ART-x` creates tasks from the ```` ```yaml orc-tasks ```` block of an accepted plan |
| `orc set ID k=v ...` | edits fields (values parsed as YAML: `[a,b]`, `true`); **never** changes `status, level, attempts, failure_class, needs_user, resume_status, parent, workflow, …` |
| `orc advance ID --to X` | changes status by the transition table: TASK `pending/ready→dispatched` (blocked_by done), `dispatched→ready`, `blocked(needs_user)/failed→ready`, researcher `done→dispatched`; ART `draft→accepted/rejected` (except research ARTs: only through `orc approve`); DEC: none — only `orc approve`; K root tasks: the K table |
| `orc new decision` | creates only `proposed` DECs; `--status accepted/superseded` is refused. `orc set` cannot edit `summary/scope/supersedes` of an accepted DEC |
| `orc show ID [--meta] [--pack]` | `--meta` for A; `--pack` for the reviewer |
| `orc approval request ID...` | a binding per ID — TASK: `plan_hash + write_scope_hash + level + risk_flags`; DEC: content; ART: body — issues a single-use nonce and prints the line the user must type, `orc:approve ID1,ID2 NONCE` (one approval for several IDs) |
| `orc approve ID...` | only when the UserPromptSubmit hook recorded exactly that line from the user after the request, the nonce is unused and no binding changed: DEC `proposed→accepted` (the superseded DEC becomes superseded), ART `draft→accepted`, TASK records the approval; `state/approvals.yaml` + journal. **The only way to create an accepted DEC** |
| `orc approval check TASK` | is the approval still valid? plan/scope/level/risk changed ⇒ expired (exit 3) |
| `orc verify TASK` | deterministic V1/V2: compares the files changed since the checkpoint (created on `advance → dispatched` of a coder task, including a snapshot of the config's `verify:` section) with write_scope, scans risk_paths/secrets, runs `test/typecheck/lint/build` from the snapshot (env without secret-like variables, cwd inside the project, timeout); creates EVD `by: orc` and prints `MISMATCH` against self-reported EVD. Each command: pass / `TEST|LINT|BUILD|TYPECHECK_FAILURE` / `ENVIRONMENT_FAILURE` (blocked by Application Control, missing native binding, command not found, exit 126/127 — EVD `info`, not a MISMATCH). Exit 0 PASS, 6 FAIL, 7 INCOMPLETE (environment errors only). Does not change the task status |
| `orc next` | promotes tasks, prints READY / REVIEW / ASK USER / NEXT (and `K:` hints for root tasks) |
| `orc apply FILE|-` | loads an envelope, routes it, prints `ROUTE:` |
| `orc handoff open|check` | opens/checks the session handoff |
| `orc gate ART-x` | can the plan proceed on its own: `PROCEED`, or `ASK` + reasons (exit 3): questions under `## Decisions for the user`, proposed decisions of the task/plan, missing ACs or orc-tasks, an untouched template |
| `orc ask ART-x` | extracts the understanding + questions of a research ART, verbatim, for the user |
| `orc report TASK-x` | deterministic report for a request: plan, task table, evidence, review, decisions, conclusion |
| `orc resume ID` / `orc abort ID` | K root task out of BLOCKED / cancel a task, after the user typed `orc:resume` / `orc:abort` |
| `orc triage run|show|downgrade ID` | triage in shadow mode (§6c) |
| `orc validate [--handoff] [--key K]` | refuses: bad frontmatter, duplicate IDs, summary > 200, references to missing IDs, invalid enums, orphan files, a broken journal |

## 8b. Guard, approvals, SAFE_MODE (shadow)

- The **UserPromptSubmit** hook `hooks/prompt_log` → `runtime/prompts.jsonl`: ts, session, sha256 of the user's prompt, and verbatim only
  the `orc:...` lines; a hash chain. The **PreToolUse** hook `hooks/guard` → `runtime/guard-shadow.jsonl` (shadow: always
  allows). The guard reads `runtime/active.json` (written by orc on every registry save) and never loads YAML.
- `orc advance <coder task> --to dispatched` without a valid approval ⇒ recorded in `runtime/shadow.jsonl` (not blocked yet).
- `ORC_SAFE_MODE=1` (set before opening the editor): the guard only allows Edit/Write to the kit + `.orchestra/config.yaml`; `orc`
  refuses commands that change the workflow; the brief shows a warning. There is no variable that turns the guard off.
- **Bash is not a semantic sandbox.** The Bash guard only denies high-confidence rules (git
  push/merge/rebase/reset --hard/clean -f/branch -D, `supabase db push|reset`, deploy/publish, recursive delete, psql to a non-localhost
  host, writes into deny_always areas, touching secret files) on the command with heredoc bodies and quoted phrases removed. It does not infer
  write_scope from Bash; `orc verify` checks afterwards. Edit/Write: deny_always (`.orchestra/` except the `writable` list of active.json,
  `.claude/`, `~/.claude/`, `.git/`, secret files, `guard.protected_paths`) + write_scope while a coder task runs.
- Path matching (write_scope, risk/guard/artifact paths): gitignore-style, anchored at the project root — `*` does not cross `/`,
  `**` crosses levels, a trailing `/` is a directory (`orc_guard.match_path`).
- Journal: a state-writing `orc` command that is refused ⇒ a line `{event: rejected, task, operation, reason, reason_hash, actor, ts}`.

## 9. Claude Code integration

- The plugin's `hooks/hooks.json` registers SessionStart, Stop, UserPromptSubmit and PreToolUse (`*`), each running
  `python "${CLAUDE_PLUGIN_ROOT}/hooks/<name>"`; stdin is JSON with `session_id`, `cwd`.
- SessionStart: `hookSpecificOutput.additionalContext` is added to the context; it cannot block the session.
- Stop: blocks with the JSON `{"decision": "block", "reason": "..."}` (exit 0).
- Subagents are namespaced by the plugin: `orchestra:researcher`, `orchestra:coder`, `orchestra:reviewer`, `orchestra:context-builder`.
  Only a subagent's final answer reaches the main session.
- Commands: `/orchestra:<name>`; frontmatter `description, argument-hint, allowed-tools, disable-model-invocation`; `$ARGUMENTS`.

## 10. Graph & Context Builder

**The graph is an optional accelerator.** Not configured / not installed / not built / failing ⇒ Orchestra runs as before (fallback).

- **Graph Adapter** `cli/graph_adapter.py` (stdlib): `is_available, status, update, query, affected, explain, path`.
  Current engine: Graphify, called as `<venv>/python -m graphify extract <root> --code-only --out <cache>`; no
  `graphify install`, no hooks, no `graphify update` (which writes into the source directory). Reading the graph is done by the adapter on
  `graph.json` ⇒ switching engines only replaces build/load.
- **Install** (optional): create a venv at `%LOCALAPPDATA%\orchestra-kit\graphify-venv` (Windows) or
  `~/.local/share/orchestra-kit/graphify-venv` and install `graphifyy[sql]==0.9.76` into it.
- **Enable per project** — `config.yaml`:
  ```yaml
  graph:
    engine: graphify
    max_files: 12        # CTX budget
    max_results: 40
    hub_degree: 25       # symbols with > 25 dependency edges are not expanded
    roots:               # built per root, so a parent .gitignore cannot exclude a directory
      - {path: app, include: [src, tests]}
      - {path: supabase/migrations}
  ```
- **Cache** `.orchestra/cache/graph/` (gitignored; the raw graph is never committed).
- **States** `none|fresh|stale|degraded|locked|unavailable`; only `fresh` is `authoritative`.
  Freshness = MD5 of each included file against the manifest (no mtime, no git). Editing docs/.orchestra does not make it stale.
  `degraded` when: the engine skipped files, .sql files exist but there are no SQL nodes, or the engine version differs from the pinned one.
- **Safe reading**: a redefined SQL function ⇒ only the one in the newest migration, older ones get `superseded_by`;
  the graph does not cover string-based RPC calls (TS → SQL) ⇒ "no path" ≠ "no dependency"; a lock file during updates,
  reading while locked ⇒ `locked` ⇒ fallback; community IDs are never stored.
- **CTX** (`contexts.yaml`): `source (manual|graph|fallback)`, `graph_state`, `fingerprint {graph, files: {path: md5}}`,
  `truncated`. The Context Builder writes the CTX body (the ```` ```yaml orc-context ```` block + Graph evidence + Risk areas +
  Context limits); A runs `orc ctx seal` (orc writes the registry). `source: graph` is refused when the graph is not fresh.
  The coder runs `orc ctx check CTX-x` before using it (files changed after the context was built).

| Command | Does |
|---|---|
| `orc graph status` | state + changed files + problems |
| `orc graph update [--force]` | rebuilds when code/SQL changed; does nothing when fresh; error ⇒ exit 4 |
| `orc graph query T...` / `affected X` / `explain X` / `path A B` | results with `state`, `authoritative`, `truncated` |
| `orc graph view [--open]` | exports an HTML map into the cache |
| `orc docs index` / `orc docs check` | regenerates the automatic part of `docs/INDEX.md` from frontmatter / checks frontmatter, the INDEX, and broken `docs/…` paths in markdown and code comments (works without `.orchestra/`) |
| `orc ctx seal CTX-x` / `orc ctx check CTX-x` | registers a CTX / reports changed files (exit 5) |

## 11. Later

Several Coders in parallel with git worktrees · docs/PDF in the graph · an enforcing guard mode.
