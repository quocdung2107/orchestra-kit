# {{project_id}}

Goal: {{goal}}

## Orchestra
- Project state lives in `.orchestra/` (only `orc` writes `.orchestra/state/`).
- A brief is loaded automatically at the start of each session. When a request creates or works on tasks: open a session with
  the command in the brief (`orc handoff open ...`), and fill in `.orchestra/sessions/S-x.md` before finishing.
- New request: `/orchestra:auto <request>`. Step by step: /orchestra:plan /orchestra:run /orchestra:review /orchestra:next /orchestra:handoff.
