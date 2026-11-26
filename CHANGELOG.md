# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.4.0] — 2026-11-24

- **A model per subagent.** researcher and reviewer `opus`, coder and context-builder `sonnet` (agent frontmatter).
  `orc model TASK [--role …]` and `model=` on `orc next` lines give the alias the Orchestrator passes when it calls the
  subagent; a coder task sent back by the reviewer (attempts ≥ 1) gets `coder_retry` (`opus`). Override with `models:` in
  the config. `orc advance … --to dispatched --model M` records the model in the journal; `orc report` has a Model column.
- **Plans live in ART files only.** `orc docs index` adds a "Plans (orcgate)" group linking the accepted plan/research ARTs
  (latest version); the researcher never copies a plan into `docs/`; `/orcgate:auto` refreshes the index at the end.
- **Config checked up front**: `orc validate` and the brief report bad `verify.commands` names (with a hint to use `lint`),
  `models` and `guard` sections, instead of failing at the first dispatch.
- **No false `approval_missing`**: K child tasks are no longer checked against a per-task approval (K binds the root).
- **Opt-in enforcing guard rules** `guard.enforce: [edit_write, git_push]` (+ `guard.always_writable`); the hook exits 2
  with the reason. Everything else stays shadow. Shadow false positives fixed: `git merge-base` is not a merge, `sed`
  without `-i` does not write.
- **One overlapping K request at a time**: entering IMPLEMENTING is refused while another request with an overlapping
  write_scope is being implemented (disjoint scopes only warn). The brief marks requests/dispatched tasks idle > 7 days as stale.
- **Subagents run in the foreground.** The commands tell the Orchestrator to call every subagent with
  `run_in_background: false` and to fix an envelope with a new foreground call instead of SendMessage, so a run no longer
  stalls waiting for a background notification that never arrives.

## [0.3.0] — 2026-11-07

- **Renamed the plugin `orchestra` → `orcgate`** (display name "Orcgate"), so it can be listed in Anthropic's plugin
  directory, where the name `orchestra` is taken. Commands are now `/orcgate:auto`, `/orcgate:plan`, …; subagents are
  `orcgate:researcher`, `orcgate:coder`, … The marketplace (`orchestra-kit`), the `orc` CLI and the project state
  directory `.orchestra/` keep their names. `marketplace.json` has `renames: {"orchestra": "orcgate"}` so existing
  settings migrate (run `/plugin install orcgate@orchestra-kit` once after updating); the guard and the legacy-hook
  bridge accept both names.
- **Python is found on every OS**: hooks and `bin/orc` start through `scripts/py`, which tries `$ORCGATE_PYTHON`,
  `python3`, `python` and `py -3` (skipping the Windows Store alias) and caches the choice in `$CLAUDE_PLUGIN_DATA`.
  Before, every hook called `python`, which does not exist on macOS or on a default Debian/Ubuntu, so the hooks failed
  and approvals were not recorded. Without Python, a hook now exits 0 and prints one warning in orcgate projects.
  `bin/orc.cmd` tries `python`, then `py -3`.
- **A broken prompt hook is named in the error**: `orc approve`, `orc resume` and `orc abort` add a hint when not a
  single prompt was recorded since the request.
- The two-stage workflow moved from `commands/auto-legacy.md` to `references/auto-legacy.md`, so it no longer shows
  up as a command.
- Directory listing metadata: `displayName`, `homepage`, `keywords`, an icon, and a README in the plugin folder.
  README: requirements per OS, "What the plugin runs and stores", troubleshooting, updating.

## [0.2.0] — 2026-10-17

First public release, packaged as a Claude Code plugin (`orchestra@orchestra-kit`).

- **Plugin layout**: marketplace `orchestra-kit` with one plugin, `orchestra`, in `plugins/orcgate/`.
  Commands are `/orchestra:auto`, `/orchestra:plan`, `/orchestra:run`, `/orchestra:review`, `/orchestra:next`,
  `/orchestra:handoff`; subagents are `orchestra:researcher`, `orchestra:coder`, `orchestra:reviewer`,
  `orchestra:context-builder`. The `orc` CLI is on `PATH` (via the plugin's `bin/`) while the plugin is enabled.
- **Hooks** (`hooks/hooks.json`): SessionStart (brief), Stop (handoff check), UserPromptSubmit (prompt log for approvals),
  PreToolUse (guard, shadow mode). Every hook is a no-op outside projects with `.orchestra/`.
- **Workflow K** (opt-in with `workflow: k`): a state machine for requests with one approval per request
  (`orc:approve <IDs> <NONCE>` typed by the user), deterministic verification (`orc verify`) before a task can be done,
  recovery table (repair / rollback / escalate) and `orc resume` / `orc abort`.
- **Two-stage workflow** for projects without `workflow: k` (`commands/auto-legacy.md`).
- **Triage** (shadow): deterministic level L0–L3 from paths, size, a declaration form and risk keywords.
- **Optional code graph** through a Graph Adapter (Graphify engine) with a text-search fallback.
- **English only**: prompts, templates, CLI output, documentation and tests. Artifact and handoff section headings
  are English (`## Decisions for the user`, `## Next steps`, …); there is no compatibility with non-English headings.
- **Known limitations**: the guard runs in shadow mode only (it logs, it never blocks); on Windows `python` must be on `PATH`
  with PyYAML installed.
