# Methodology: the appmap loop

How the orchestrator drives the `appmap` CLI, how to read its output, when to rerun, when to stop,
and how to resume. `appmap` always means the plugin wrapper `scripts/appmap`, run by absolute path.

## The APPMAP_STATUS contract

Every subcommand prints, as its **last stdout line**:

```
APPMAP_STATUS {"command": "crawl", "ok": true, "status": "in_progress", "counts": {"states": 41, "transitions": 77, "forms": 9, "exchanges": 612, "queued_approvals": 3}, "pending_approvals": 3, "errors": [], "next": "appmap crawl --run /work/appmap-staging.example.com-20261024-120000"}
```

| Key | Meaning |
|---|---|
| `command` | The subcommand that ran |
| `ok` | `true` when the command did its job (exit code 0); `false` otherwise (exit code 1) |
| `status` | What state the command ended in (table below) — branch on this |
| `counts` | Numbers for this command (keys differ per command) |
| `pending_approvals` | Held actions still waiting for a human decision |
| `errors` | Human-readable problems and warnings (a role that failed to log in, rules skipped in `approvals.yaml`, a tripped circuit breaker, …). Can be non-empty when `ok` is `true` |
| `next` | Suggested next command (bare `appmap …`; run it through the wrapper), a short instruction, or `null` |

How to read it:

1. Take the last line of stdout that starts with `APPMAP_STATUS `; parse the rest as JSON.
2. If there is no such line and the exit code is 2, the command line itself was wrong (argument
   error printed by the parser). Check `appmap <subcommand> --help` and fix the call.
3. Lines printed **before** the status line carry data: `init` prints the run directory path;
   `status --gaps` prints one `{"gaps": {…}}` line; `approvals list` prints one JSON object per
   pending item; `query` prints one JSON object per row (or a table with `--format table`).
4. stderr may hold first-run setup output or Python warnings. `crawl.log` in the run directory is
   for humans; use it only to explain an error.

### Status values per command

| Command | `status` values | Meaning / what to do |
|---|---|---|
| `init` | `initialized` | Run directory created (path on the previous line and in `next`) |
| | `config_error` | Config invalid; `errors` says why. Fix the file, rerun `init` |
| `auth` | `authenticated` | Every selected role logged in |
| | `auth_failed` | At least one role failed (`errors` lists them; `counts.roles_ok`) |
| | `error` | Command failed (bad run dir, unknown role, browser problem) |
| `crawl` | `in_progress` | Budget used up, frontier not empty. **Rerun the same command** |
| | `done` | Frontier exhausted for the selected roles |
| | `limit_reached` | Cumulative crawl time hit `limits.max_runtime_minutes`. Stop crawling |
| | `paused_circuit` | Too many 429/5xx from the target. **Stop and tell the operator** |
| | `auth_failed` | A role's login or mid-crawl re-login failed (`errors` names it; at most 2 re-logins in a row, a failed one is never repeated). That role stopped with its pending items kept; other roles ran. `ok` is false; run `next` (`appmap auth … --role R`, with the operator for manual roles), then rerun crawl |
| | `error` | Unexpected failure; `errors` and `crawl.log` explain |
| `crawl --approved-only` | `done` / `in_progress` / `paused_circuit` / `auth_failed` | Approved items executed / budget used, rerun with `--approved-only` / stop and tell the operator. `counts` has `executed`, `failed`, `skipped` |
| `probe` | `in_progress` | Budget used up; rerun the same command |
| | `done` | Phase finished |
| | `paused_circuit` | Stop and tell the operator |
| | `error` | Unexpected failure |
| `approvals` | `listed` / `written` / `applied` | For `list` / `write` / `apply`. `counts`: `pending`, `approved`, `denied`, `skipped`, `executed`, `approved_unexecuted`, `failed`, `changed` |
| `build` | `complete` | Map written. `counts`: `flows`, `flow_steps`, `value_links`, `preconditions`, `names_applied`, `forms`, `endpoints`, `routes`, `states` |
| `status` | `reported` | Plain report |
| | `new_pass` | `--new-pass` closed a pass: read `counts.prev_pass` and `counts.new_in_prev_pass` |
| | `no_change` | `--new-pass` refused: nothing ran since the current pass began |
| `query` | `complete` / `error` | `counts.rows`, `counts.recipe`; `sql` also `counts.truncated` (rows beyond `--limit`, default 1000) |
| any | `usage_error` | Bad arguments (argparse message in `errors`; usage on stderr). Nothing ran; fix the command (`next` is its `--help`) |
| `auth`, `crawl`, `probe`, `approvals`, `build` | `locked` | Another of these commands is running on the same run directory (`<run>/.lock`). `ok` is false and nothing was done. Wait for the other command to finish, then rerun (`next`). Never run two at once |

`appmap status --run <run>` counts: `states`, `transitions`, `endpoints`, `routes`, `exchanges`,
`forms`, `approvals` (same keys as the approvals command), `pass`, `new_in_pass`,
`pending_frontier`, plus `prev_pass`/`new_in_prev_pass` with `--new-pass` and `gaps` (list sizes)
with `--gaps`.

## Budgets and reruns

`crawl` and `probe` stop cleanly after `--budget-minutes` (default 8) and report `in_progress`.
All progress lives in the run directory, so rerunning the identical command continues where it
stopped. Keep rerunning until the status is something else. If your harness limits how long one
command may run, pass a smaller budget (for a 10-minute limit use `--budget-minutes 5`) or run
the command in the background and wait for it.

Separately, `limits.max_runtime_minutes` (default 240) caps the **total** browser-crawl time of
the run; reaching it gives `limit_reached`, which counts as a stop condition for the whole loop.

## The loop

| Step | Command(s) | Phase skill | Leave the step when |
|---|---|---|---|
| 1 | `appmap init --config <cfg> --out <dir>` | `configure-crawl` | `initialized` |
| 2 | `appmap auth --run <run>` (per role for manual roles) | `authenticate-roles` | all roles `auth_ok = 1` |
| 3 | `appmap probe --run <run> --phase passive` | `probe-endpoints` | `done` |
| 4 | `appmap crawl --run <run>` | `crawl-application` | `done` or `limit_reached` |
| 5 | `appmap probe --run <run> --phase active` | `probe-endpoints` | `done` |
| 6 | `appmap approvals --run <run> write` … `apply`, `appmap crawl --run <run> --approved-only`, `appmap crawl --run <run>` | `review-approvals` | no pending items, approved ones executed |
| 7 | `appmap status --run <run> --new-pass`, `appmap status --run <run> --gaps` | (this skill) | stop rule met |
| 8 | `appmap build --run <run>` | `build-app-map` | `complete` with names applied |

Approval checkpoints: held actions are queued while the crawl continues elsewhere. Review them
(Step 6, `review-approvals`) **when a crawl pass drains** (crawl reports `done` with
`pending_approvals > 0`, end of Step 4) and **again after the active probe** (end of Step 5), plus
whenever a later pass adds more. Logout actions are held and never executed, even when approved.

## Double back

The goal is convergence, not a fixed number of crawls. The CLI tracks passes; you decide.

Call order matters:

1. Run the pass's work (crawl / probe / approved execution).
2. `appmap status --run <run> --new-pass` — closes the current pass and opens the next. It
   reports `status: "new_pass"`, `counts.prev_pass` (the pass just closed) and
   `counts.new_in_prev_pass` = `{"states": n, "endpoints": n, "routes": n}` discovered during it.
3. Apply the **stop rule**: stop when every value in `new_in_prev_pass` is 0, **or** `prev_pass`
   ≥ 3, **or** a crawl reported `limit_reached`.
4. Not stopped: `appmap status --run <run> --gaps` and read the `{"gaps": …}` line:
   - `unexercised_endpoints` — known only from bundles/schemas/robots/sitemaps, never requested.
     Actionable via `probe --phase active` (if it has not run since they appeared), or an
     approval if they were held.
   - `unsuccessful_forms` — `attempted: true` failed validation; `attempted: false` never
     submitted (often held for approval). Better fixtures help forms not yet attempted; see
     `crawl-application`.
   - `unreached_routes` — routes no role reached. A missing role, a deny pattern, or a route only
     reachable after an approved action.
   - `unexplored_actions` — frontier items still `pending` (crawl again) or `failed`; approved
     items not yet executed (`approved_pending`) or failed with a `reason`.
   - `pending_approvals` / `denied_approvals` — held surface; pending ones go to the human.
5. Pick only actionable gaps, say why, and run one targeted pass. If nothing is actionable, stop
   and record why in the final report.
6. Return to step 2. `status: "no_change"` means nothing ran since the pass began — a repeated
   `--new-pass` cannot fake convergence. Run the targeted work first, or stop.

Do not loop on gaps you cannot change (e.g. endpoints only visible in a bundle that every role
gets 404 for, or forms whose required data only the operator can supply).

## Resuming a run

Everything lives in the run directory; the config is re-read from `run.json`'s `config_path` on
every command (keep that file in place). To resume:

1. Read `<run>/run.json` (`status`, `profile`, `roles`, `authorization`) and run
   `appmap status --run <run>`.
2. Choose the step: go down the table and take the **first** row whose evidence matches.

| Evidence (first match wins, top to bottom) | Continue at |
|---|---|
| `run.json` status `initialized` or `auth_failed` | Step 2 |
| `run.json` status `authenticated` | Step 3 (passive probe; skip to Step 4 if the operator or `crawl.log` shows it already finished) |
| `run.json` status `paused_circuit` | Ask the operator whether the target is healthy; then Step 4 |
| `run.json` status `in_progress`, or `counts.pending_frontier > 0` | Step 4 (rerun crawl) |
| `counts.approvals.pending > 0` | Step 6 |
| `counts.approvals.approved_unexecuted > 0` | Step 6 (`crawl --approved-only`) |
| `role_access` empty (`appmap query --run <run> role-matrix` returns no rows) | Step 5 |
| `run.json` status `complete` | Step 8 review, or Step 7 if the operator wants more coverage |
| otherwise | Step 7 |

`run.json` status is written by `init`, `auth`, `crawl` and `build` only. `probe` does not write
it, so a probe that stopped with `paused_circuit` or `in_progress` is visible only in
`crawl.log` (search it for `probe` and `circuit`) or from the operator — ask before assuming the probe
finished, and rerun that probe phase if in doubt (already-sent active requests are not repeated).

Saved sessions live in `~/.cache/absolute-appmap/sessions/<run_id>/`. If they have expired
(login failures in `errors`), rerun Step 2 for those roles first.

## Judgment you own

| Decision | Where |
|---|---|
| Authorization and scope confirmation; the config interview | `configure-crawl` |
| Domain fixtures when generated data keeps failing validation | `crawl-application` |
| Confirm / downgrade / add held actions, with reasons; present to the human | `review-approvals` |
| Flow names, descriptions and form purposes | `build-app-map` |
| Which gaps deserve a targeted pass; when coverage is good enough | Step 7 above |

Everything else is mechanical and belongs to the CLI. If you find yourself wanting to click
through the app or send requests by hand, stop: that bypasses scope, throttling, approvals and
redaction.
