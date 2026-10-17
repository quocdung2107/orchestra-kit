# orchestra-kit

**orchestra** is a Claude Code plugin for multi-agent work on a codebase. The main session acts as an
Orchestrator and hands work to four subagents — researcher, coder, reviewer and context-builder — while all
project state (tasks, plans, decisions, evidence, approvals) lives in plain files under `.orchestra/` and is
written only by a small deterministic CLI, `orc`. Agents pass IDs to each other instead of long chat, and
anything the model cannot be trusted with (state transitions, approvals, verification) is done by `orc`.

Design reference: [SCHEMA.md](SCHEMA.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## Requirements

- Claude Code (CLI or an IDE extension) with plugin support
- Python 3.10+ with PyYAML: `python -m pip install --user pyyaml`
- git
- On Windows, `python` must be on `PATH` (the hooks run `python "<plugin>/hooks/…"`)

## Install

This repository is a plugin marketplace. In Claude Code:

```
/plugin marketplace add quocdung2107/orchestra-kit
/plugin install orchestra@orchestra-kit
```

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
    "orchestra@orchestra-kit": true
  },
  "permissions": {
    "allow": ["Bash(orc *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)"]
  }
}
```

Restart Claude Code afterwards. Type `/orchestra:` to see the commands.

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
| `/orchestra:auto <request>` | the whole loop for one request |
| `/orchestra:plan <request>` | only plan it |
| `/orchestra:run [TASK]` | dispatch the next ready task |
| `/orchestra:review <TASK>` | review a task with the reviewer subagent |
| `/orchestra:next` | what is next and what needs your decision |
| `/orchestra:handoff` | write the session handoff |

Subagents: `orchestra:researcher`, `orchestra:coder`, `orchestra:reviewer`, `orchestra:context-builder`.

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
- Windows: `python` must be on `PATH` with PyYAML installed, otherwise the hooks print an error (they never break the session).
- The `orc` command is on `PATH` only inside Claude Code sessions with the plugin enabled.

## Uninstall

```
/plugin uninstall orchestra@orchestra-kit
/plugin marketplace remove orchestra-kit
```

Remove the `enabledPlugins` / `extraKnownMarketplaces` entries from any project `.claude/settings.json`.
A project's `.orchestra/` directory is plain files; delete it if you no longer want the history.

## Development

```
python -m unittest discover -s tests -v
claude plugin validate .
claude plugin validate plugins/orchestra
python tools/release_check.py
```

`tools/release_check.py` fails on local paths, e-mail addresses and non-English text; put personal patterns in
`tools/denylist.txt` (gitignored).

## License

[MIT](LICENSE)
