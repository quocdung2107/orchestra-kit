# Orcgate

**Orcgate** (`orcgate`) is a Claude Code plugin for multi-agent work on a codebase. The main session acts as an
Orchestrator and hands work to four subagents — researcher, coder, reviewer and context-builder — while all
project state (tasks, plans, decisions, evidence, approvals) lives in plain files under `.orchestra/` and is
written only by a small deterministic CLI, `orc`. Agents pass IDs to each other instead of long chat, and
anything the model cannot be trusted with (state transitions, approvals, verification) is done by `orc`.
Each request needs one explicit approval line that only you type.

This repository (`orchestra-kit`) is the plugin's marketplace. The plugin was called `orchestra` before 0.3.0.
Design reference: [SCHEMA.md](SCHEMA.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## Requirements

- Claude Code (CLI, desktop app or an IDE extension) with plugin support. The plugin works only in Claude Code:
  claude.ai and Cowork do not install plugins that ship executables in `bin/`.
- Python 3.10+ with PyYAML. The plugin looks for `python3`, then `python`, then the Windows launcher `py -3`
  (set `ORCGATE_PYTHON` to a full path to choose another one).
  - macOS: `python3` from the Xcode Command Line Tools or Homebrew; then `python3 -m pip install --user pyyaml`
    (Homebrew Python: `brew install pyyaml` or a virtual environment).
  - Debian/Ubuntu: `sudo apt install python3 python3-yaml`.
  - Windows: Python from python.org (tick "Add python.exe to PATH") or the `py` launcher; then
    `py -3 -m pip install --user pyyaml`.
- git. On Windows, Git for Windows: Claude Code runs the hooks through its Git Bash.

## Install

In Claude Code:

```
/plugin marketplace add quocdung2107/orchestra-kit
/plugin install orcgate@orchestra-kit
```

Or from a shell: `claude plugin install orcgate --marketplace quocdung2107/orchestra-kit`.

Or declare it per project, so everyone who opens the project gets the same setup. Add to the project's
`.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "orchestra-kit": {
      "source": { "source": "github", "repo": "quocdung2107/orchestra-kit" }
    }
  },
  "enabledPlugins": {
    "orcgate@orchestra-kit": true
  },
  "permissions": {
    "allow": ["Bash(orc *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)"]
  }
}
```

A plugin cannot grant itself permissions, so the `permissions` block is yours to add (without it, Claude asks
before each `orc` command). Start a new session afterwards and type `/orcgate:` to see the commands.

## Set up a project

The plugin does nothing in a project until it has an `.orchestra/` directory. Inside a Claude Code session in the
project, run (or ask Claude to run):

```
orc init --project-id my-app --goal "One sentence describing what the project is for."
```

This creates `.orchestra/` (registries, templates, a `.gitignore` for runtime files) and a `CLAUDE.md` if the
project has none. Commit `.orchestra/` together with your code.

To use **Workflow K** (one approval per request, verification before a task can be done), add to
`.orchestra/config.yaml`:

```yaml
workflow: k
verify:
  repos: ["."]
  timeout: 900
  commands:
    test: {run: "npm test"}
    build: {run: "npm run build"}
    # typecheck / lint are also supported; each entry may set cwd: <dir>
```

Without `workflow: k` the plugin uses the simpler two-stage workflow (understand & design → plan → code → review).

## Use

Start a new session in the project: the brief (goal, open tasks, decisions waiting for you) is loaded automatically.

| Command | What it does |
|---|---|
| `/orcgate:auto <request>` | the whole loop for one request |
| `/orcgate:plan <request>` | only plan it |
| `/orcgate:run [TASK]` | dispatch the next ready task |
| `/orcgate:review <TASK>` | review a task with the reviewer subagent |
| `/orcgate:next` | what is next and what needs your decision |
| `/orcgate:handoff` | write the session handoff |

Subagents: `orcgate:researcher`, `orcgate:coder`, `orcgate:reviewer`, `orcgate:context-builder`.

### Workflow K and approvals

1. **Understand & design** — the researcher restates the request, proposes a design and lists questions.
   The Orchestrator shows them to you and stops. Your answers become proposed decisions.
2. **Plan** — the researcher writes a plan with acceptance criteria and small tasks. `orc gate` decides whether it
   can proceed or must ask you.
3. **Approval** — `orc` prints one line such as `orc:approve TASK-0001,ART-0002,DEC-0001 5DA20D`. Only **you** type it
   (the UserPromptSubmit hook records it, and `orc approve` checks the exact line, a single-use nonce and that nothing
   changed since the request). Agents are told never to type it for you.
4. **Code → verify → review** — coder tasks run only after approval; `orc verify` checks the changed files against
   each task's `write_scope` and runs your `verify:` commands; the reviewer judges each acceptance criterion.
5. **Report** — one final report. Code stays on the branch `orchestra/<TASK>`; nothing is merged or pushed.

When something needs you, the Orchestrator stops with one question. Other lines only you type:
`orc:resume <TASK>` (continue a blocked request), `orc:abort <TASK>` (cancel), `orc:downgrade <TASK> L<n> <reason>`.

### Optional code graph

`orc graph …` can use [Graphify](https://pypi.org/project/graphifyy/) to help the context-builder pick files. It is
optional; without it, a text-search fallback is used. See SCHEMA.md §10.

## Known limitations

- The guard (PreToolUse hook) runs in **shadow mode**: it records what it would deny in `.orchestra/runtime/guard-shadow.jsonl`
  but never blocks. `orc verify` is the actual check, after the fact.
- Triage levels are computed in shadow mode as well; every request currently needs an explicit approval.
- Bash is not a sandbox: a script can still write outside `write_scope`; `orc verify` reports it afterwards.
- Without a usable Python the hooks do nothing except print one warning (they never break the session), so approvals cannot be recorded; see Troubleshooting.
- The `orc` command is on `PATH` only inside Claude Code sessions with the plugin enabled.

## What the plugin runs and stores

Everything below happens on your machine. The plugin has no network access of its own, sends nothing anywhere
and does not change Claude Code's settings or permissions.

| When | What runs | What it writes |
|---|---|---|
| Session start | `hooks/session_start`: builds the brief with `orc brief` and adds it to Claude's context | nothing |
| Every prompt you send | `hooks/prompt_log` | `.orchestra/runtime/prompts.jsonl`: time, session id, a SHA-256 of the prompt, and verbatim only the lines that start with `orc:` (approvals). The rest of the prompt is not stored |
| Every tool call | `hooks/guard` (shadow mode, never blocks) | `.orchestra/runtime/guard-shadow.jsonl`: tool name, target path or command, the decision it would take |
| Before Claude finishes | `hooks/stop_check`: checks the session handoff | nothing |
| When Claude runs `orc` | the `orc` CLI (`bin/orc`) | files under `.orchestra/` only |
| `orc verify` | `git` plus the shell commands **you** list under `verify:` in `.orchestra/config.yaml` | evidence entries under `.orchestra/`, logs in `.orchestra/runtime/verify/` |
| `orc graph …` (optional) | Graphify from its own virtual environment, if you installed it | `.orchestra/cache/graph/` |

Every hook first looks for `.orchestra/config.yaml` and does nothing in projects without it. The hooks are started
by `scripts/py`, which finds Python and caches its choice in `~/.claude/plugins/data/` (`python-cmd`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A "hook error" notice, or the brief says `Python 3.10+ not found` | no usable Python for the hooks | install Python 3.10+ (see Requirements) or set `ORCGATE_PYTHON`, then start a new session |
| `orc approve` says nothing was typed although you typed the line, and adds "the UserPromptSubmit hook is not running" | the prompt hook did not run (no Python, or the plugin is disabled in this project) | fix Python, check `/plugin` shows `orcgate@orchestra-kit` enabled, start a new session, ask for a new approval |
| The brief says `PyYAML is missing` | PyYAML not installed for the Python the plugin uses | install it for that Python (see Requirements) |
| `orc: command not found` in your own terminal | `bin/` is on `PATH` only inside Claude Code sessions | run it through Claude, or call `python3 ~/.claude/plugins/cache/orchestra-kit/orcgate/<version>/bin/orc` |
| Two briefs, or every prompt logged twice | an older manual install still has hooks in `~/.claude/settings.json` or `.claude/settings.local.json` | remove those hook entries; the plugin's own hooks are enough |
| `/orchestra:…` commands are gone | the plugin was renamed to `orcgate` in 0.3.0 | use `/orcgate:…`; update `enabledPlugins` to `orcgate@orchestra-kit` |

## Update

```
claude plugin update orcgate@orchestra-kit
```

or turn on auto-update for the `orchestra-kit` marketplace in `/plugin` → Marketplaces. Start a new session (or run
`/reload-plugins`) afterwards. Installs of the former name `orchestra@orchestra-kit` are migrated to `orcgate` by the
marketplace's `renames` entry; project files that still say `orchestra@orchestra-kit` should be updated by hand.

## Uninstall

```
/plugin uninstall orcgate@orchestra-kit
/plugin marketplace remove orchestra-kit
```

Remove the `enabledPlugins` / `extraKnownMarketplaces` entries from any project `.claude/settings.json`.
A project's `.orchestra/` directory is plain files; delete it if you no longer want the history.

## Development

```
python -m unittest discover -s tests -v
claude plugin validate .
claude plugin validate plugins/orcgate
python tools/release_check.py
```

`tools/release_check.py` fails on local paths, e-mail addresses and non-English text; put personal patterns in
`tools/denylist.txt` (gitignored).

## License

[MIT](LICENSE)
