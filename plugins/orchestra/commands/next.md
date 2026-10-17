---
description: Orchestra — show the next task and anything the user needs to decide
allowed-tools: Bash(orc *)
disable-model-invocation: true
---

Run `orc next` and `orc brief`. Answer the user in at most 6 lines: what the user needs to decide (if anything),
tasks in review, the next ready task and the command to run (/orchestra:run, /orchestra:review). Do not read file contents.
