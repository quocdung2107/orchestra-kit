---
description: Orchestra — write the handoff for the current session before finishing
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

1. Find the session's S-ID (the line "This session has S-x open" in the brief, or the `orc handoff open` command already run).
   If the session has no S-ID but did work worth recording → open one with the command from the brief, then write it.
2. Edit `.orchestra/sessions/<S-ID>.md`:
   - frontmatter `result`: `done` | `partial` | `blocked`; `task`: the main TASK-ID (if any).
   - Fill in all 5 sections: Done · Files changed · Decisions made · Open questions · Next steps.
     Write "None" in an empty section. Use IDs (TASK-, ART-, DEC-, EVD-) instead of retelling content.
3. `orc handoff check --key <key from the brief>` (or `--sid <S-ID>`) until it prints "handoff is valid".
4. `orc validate` must print `OK`.
