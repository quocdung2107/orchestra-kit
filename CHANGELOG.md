# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-10-17

First public release, packaged as a Claude Code plugin (`orchestra@orchestra-kit`).

- **Plugin layout**: marketplace `orchestra-kit` with one plugin, `orchestra`, in `plugins/orchestra/`.
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
