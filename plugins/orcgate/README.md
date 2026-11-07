# Orcgate

Orcgate is a Claude Code plugin for multi-agent work on a codebase. The main session acts as an Orchestrator and
hands work to four subagents (researcher, coder, reviewer, context-builder). Project state — tasks, plans,
decisions, evidence and approvals — lives in plain files under `.orchestra/` in your project and is written only by
a small deterministic CLI, `orc`. Each request needs one approval line that only you type, and `orc verify` checks
changed files and runs your test commands before a task can be done.

- Commands: `/orcgate:auto`, `/orcgate:plan`, `/orcgate:run`, `/orcgate:review`, `/orcgate:next`, `/orcgate:handoff`
- Subagents: `orcgate:researcher`, `orcgate:coder`, `orcgate:reviewer`, `orcgate:context-builder`
- Hooks: SessionStart (brief), UserPromptSubmit (records `orc:` approval lines and a hash of each prompt),
  PreToolUse (guard in shadow mode, never blocks), Stop (handoff check). All of them do nothing in projects
  without `.orchestra/`, and none of them uses the network.
- Requires Python 3.10+ with PyYAML (`python3`, `python` or `py -3`) and git. Claude Code only.

Install in Claude Code:

```
/plugin install orcgate --marketplace quocdung2107/orchestra-kit
```

Then, inside a session in your project: `orc init --project-id my-app --goal "What the project is for."`

Full documentation — requirements per OS, what the plugin runs and stores, troubleshooting and updates:
https://github.com/quocdung2107/orchestra-kit#readme

License: MIT.
