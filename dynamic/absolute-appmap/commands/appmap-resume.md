---
description: Resume a paused or interrupted absolute-appmap run from its run directory.
argument-hint: <run directory>
---

Resume an absolute-appmap run. Use the `map-running-application` skill; at Step 0 treat the input as an existing run directory and follow "Resuming a run" in its `references/methodology.md`: read `run.json`, run `appmap status --run <run>`, and continue at the step named by the **first** matching row of the resume table (top to bottom).

Run directory: $ARGUMENTS

- If `run.json` says `paused_circuit`, ask the operator whether the target is healthy before sending anything.
- `probe` does not update `run.json`: a probe that stopped (`paused_circuit` or `in_progress`) shows only in `crawl.log` or in what the operator tells you — ask before assuming it finished.
- If saved sessions have expired, re-authenticate the affected roles first (`authenticate-roles`); manual roles need the operator.
