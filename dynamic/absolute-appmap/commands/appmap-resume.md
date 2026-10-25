---
description: Resume a paused or interrupted absolute-appmap run from its run directory.
argument-hint: <run directory>
---

Resume an absolute-appmap run. Use the `map-running-application` skill; at Step 0 treat the input as an existing run directory and follow "Resuming a run" in its `references/methodology.md` to pick the step to continue from.

Run directory: $ARGUMENTS

If the run stopped with `paused_circuit`, ask the operator whether the target is healthy before sending anything. If saved sessions have expired, re-authenticate the affected roles first (`authenticate-roles`).
