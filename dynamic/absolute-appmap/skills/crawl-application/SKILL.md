---
name: crawl-application
description: Use during an appmap run, for an existing run directory (to start a new mapping use map-running-application), to crawl the running application in the browser for every role - follow links, click controls, open menus and tabs, submit forms with generated data, explore SPA client routes and states - by running appmap crawl budget by budget until the frontier is exhausted, handling in_progress, limit_reached and paused_circuit correctly, and writing fixtures.yaml when forms keep failing validation. Triggers on "crawl the app", "continue the crawl", "the crawl stopped", "forms keep failing validation", "the circuit breaker tripped".
---

## Purpose

`appmap crawl` drives Chromium through the application as each role: it builds a state graph
(URL + DOM fingerprint), records every transition, request and response, fills and submits every
form (retrying on validation errors), and holds destructive actions for approval instead of
performing them. It is resumable and budgeted: each invocation stops after `--budget-minutes`
(default 8) and the next one continues.

Your job is the loop, reading the status correctly, and one judgment call: supplying realistic
domain data (fixtures) when generated data keeps failing.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`.

**Hard rules:** `paused_circuit` → stop and tell the operator. A CAPTCHA, bot challenge or WAF
block (`observations` of kind `captcha` or `rate_limited`, a login that needs a challenge) is never
worked around — stop that part of the crawl and escalate to the operator. Never click through the
app yourself to "help" the crawler.

---

## Step 1 — Start the Crawl

`appmap crawl --run <run>`

- Crawls every role, one after another. `--role <role>` limits it to one role (e.g. a role added
  later, or to give a large role its own invocations).
- `--budget-minutes N` changes the per-invocation budget. If your harness kills commands after a
  fixed time, pick a budget a few minutes below that limit.
- Roles without a saved session are logged in first; `manual` roles must have been
  authenticated by the operator beforehand (`authenticate-roles`).
- The crawl never prompts for a manual login. If a `manual` role's session is lost mid-crawl (or
  any role's re-login fails), that role stops with `auth_failed` and its pending items kept;
  re-authenticate it with the operator (`authenticate-roles`), then rerun the crawl.

---

## Step 2 — Loop on the Status

Read the `APPMAP_STATUS` line after every invocation:

| `status` | Do this |
|---|---|
| `in_progress` | Run the **same command** again. Before rerunning, do Step 3 every few invocations |
| `done` | The frontier is exhausted for the selected roles. Go to Step 4 |
| `limit_reached` | `limits.max_runtime_minutes` total crawl time is used up. Stop crawling this run; this is a double-back stop condition. Tell the operator (they may raise the limit in the config) |
| `paused_circuit` | **Stop.** The target returned too many 429/5xx responses. Tell the operator, quote `errors`, suggest lowering `max_requests_per_second`, and wait for their go-ahead before rerunning |
| `auth_failed` | A role could not log in, or its session was lost and the re-login failed (wrong/changed password, expired manual session). That role stopped; its pending items are kept and nothing was marked failed. Run `next` (`appmap auth --run <run> --role R`; for a manual role the operator logs in by hand) via `authenticate-roles`, then rerun the crawl. Never retry the login in a loop yourself (account lockout) |
| `error` | Read `errors` (and the tail of `<run>/crawl.log`). Fix the cause (missing session, config problem) before rerunning |

Also read every time: `counts` (`states`, `transitions`, `forms`, `exchanges`,
`queued_approvals`), `pending_approvals`, and `errors` — warnings such as
`role X: … role stopped, its pending items are kept` (rerun `authenticate-roles` for X) or
`approvals.yaml: …` (a malformed decision or rule).

---

## Step 3 — Watch Form Outcomes; Write Fixtures When Needed

Between invocations, list forms that failed:

```
appmap query --run <run> sql "SELECT f.id, s.route_pattern AS page, fi.name, fi.type, fi.constraints, fi.validation_messages FROM forms f LEFT JOIN fields fi ON fi.form_id = f.id LEFT JOIN states s ON s.id = f.state_id WHERE f.succeeded = 0 ORDER BY f.id, fi.id"
```

(`validation_messages` holds the field-level messages seen; a failed form with none recorded
failed on a page-level message — look at its screenshot under `<run>/artifacts/`.)

When the same fields keep failing (messages such as "invalid VAT number", "unknown postcode",
"must be an existing customer id"):

1. Write `fixtures.yaml` next to the config with realistic values (format in
   `references/form-filling.md`). Use the operator's knowledge where it is needed (valid internal
   codes, test card numbers) — never real personal data.
2. Set `form_data.fixtures: ./fixtures.yaml` in the config (it is re-read on every command).
3. Continue the crawl. Fixtures apply to forms submitted **from now on**; forms already attempted
   in this run are not resubmitted (they stay in the gaps report). If many important forms failed
   early, tell the operator a fresh run with the fixtures configured from the start will cover
   them.

Do not add fixtures for forms that failed for other reasons (server 500s, CAPTCHA, held for
approval) — those show up as observations or approvals instead.

---

## Step 4 — Hand Off

When `done`: if `pending_approvals > 0`, this is the first approval checkpoint — run
`review-approvals` now (a drained crawl pass), then continue with `probe-endpoints` (active);
approvals are reviewed again after the active probe. The `next` field suggests
`appmap approvals --run <run> write` or `appmap probe --run <run> --phase active` accordingly.

Quick sanity check of what the crawl found:
`appmap status --run <run>` (counts) and
`appmap query --run <run> --format table sql "SELECT role, count(*) AS states FROM states GROUP BY role"`.
A role with very few states usually means its session was lost or its landing page is empty —
check `authenticate-roles` before moving on.

For SPAs, check routes were extracted:
`appmap query --run <run> sql "SELECT pattern FROM routes WHERE kind = 'spa_route'"`.
If none and the app is an SPA, see `references/spa-techniques.md`.

---

## Completion Criteria

- [ ] `appmap crawl` rerun until it reported `done` (or `limit_reached`) — never left at
      `in_progress`.
- [ ] Any `paused_circuit` reported to the operator and resumed only with their go-ahead.
- [ ] Failed forms reviewed; `fixtures.yaml` written and configured where generated data kept
      failing validation.
- [ ] Every role produced states; login warnings in `errors` resolved.
- [ ] `pending_approvals` noted for `review-approvals`.

Return to `map-running-application` Step 5 (`probe-endpoints`, active).

---

## Reference Files

- `references/spa-techniques.md` — how the SPA layer works, how to tell whether it engaged, and
  which config knobs to turn.
- `references/form-filling.md` — how forms are discovered, filled, retried and recorded; the
  fixtures format.
- `../map-running-application/references/methodology.md` — full status table and budgets.
