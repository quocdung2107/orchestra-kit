
# Plan {{id}} (task {{task}})

## Goal
<!-- 1–3 sentences: what this feature solves, for whom -->

## Approach
<!-- 2–3 short options, the chosen one and why (the simplest that is good enough) -->

## Scope
<!-- In / out -->

## Acceptance criteria
<!-- One criterion per line, with an ID that tasks reference as {{id}}#AC-n.
     Each criterion must be verifiable by a test or a concrete action. -->
- AC-1: ...

## Design
<!-- Files to change, data flow, risks. Defaults you chose yourself are written as "Assumption: ..." -->

## Decisions for the user
<!-- Only what CANNOT be decided by yourself: changing business rules, the data model, the architecture,
     a scope so vague that no reasonable default exists, or an irreversible action.
     One question per line with a recommended option. If there are none, write exactly: None -->
None

## Tasks
<!-- orc creates tasks from the block below once the plan is accepted: `orc new task --from {{id}}`.
     key: a local name for blocked_by to reference; acceptance: AC-n of this plan. -->
```yaml orc-tasks
- key: t1
  title: ...
  owner: coder
  acceptance: [AC-1]
  write_scope: ["src/..."]
  blocked_by: []
```
