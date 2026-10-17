# Context {{id}} (task {{task}})

<!-- Filled in by the Context Builder. This is a HINT for the coder/reviewer to start in the right place, not the final truth:
     run `orc ctx check {{id}}` before using it; reread from disk any file reported as changed. -->

## Selected files
<!-- The block below is read by `orc ctx seal`. source: graph (graph fresh) | fallback (no graph / stale / error).
     At most max_files files (default 12); if trimmed, truncated: true. Paths relative to the project root. -->
```yaml orc-context
source: fallback
truncated: false
files:
  - path: src/...
    reason: "..."
```

## Graph evidence
<!-- The `orc graph query/affected/explain` results used (symbol, file:line, relation). Fallback: the search commands run. -->

## Risk areas
<!-- Places that may be affected but were not selected (UI hubs, SQL/RLS reached through string-based RPC, related tests). -->

## Context limits
<!-- Graph state at build time; the graph does not cover string-based RPC (TS → SQL); SQL functions redefined
     in newer migrations; what was cut because of the budget. -->
