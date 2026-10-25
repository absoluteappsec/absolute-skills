---
name: map-running-application
description: Use when asked to map, crawl or spider a running web application (server-rendered or SPA) and produce a complete application map — pages, client routes, forms, API endpoints, GraphQL operations, user flows and a role-by-role access matrix — as input for test generation or security testing. Drives the absolute-appmap loop with the appmap CLI (Playwright plus direct HTTP) - confirm authorization, configure, authenticate roles, probe passive sources, crawl, probe actively, review destructive-action approvals with the human, double back on coverage gaps, and build APP-MAP.md plus a queryable SQLite map. Triggers on "map this app", "crawl the application", "spider staging", "build an app map", "discover every endpoint of the running app", "map the attack surface of the site", "resume the appmap run". Staging by default, throttled production on request.
---

## Purpose

This is the orchestrator. It runs the whole mapping loop against a running web application the
operator is **authorized** to test, delegating each phase to a phase skill and keeping the
judgment calls (authorization, approvals, fixtures, naming, gap review) with you and the human.

All crawling, recording, throttling, scope enforcement and redaction happen inside the `appmap`
CLI. You never write crawler code and never send requests to the target yourself: every request
must pass through the CLI's scope, throttle, approval and redaction gates. You run commands, read
their structured status, and decide.

**Conventions used by every appmap skill:**

- `appmap` means the wrapper `scripts/appmap` at the plugin root (two directories above this
  file). Run it by absolute path, e.g. `/path/to/absolute-appmap/scripts/appmap crawl --run <run>`.
  The first call installs a virtualenv and Chromium into `~/.cache/absolute-appmap/` (needs
  network, takes a few minutes; progress goes to stderr).
- `<run>` is the run directory `appmap-<host>-<YYYYMMDD-HHMMSS>/` created by `appmap init`.
- Every command's **last stdout line** is `APPMAP_STATUS {json}` with keys `command`, `ok`,
  `status`, `counts`, `pending_approvals`, `errors`, `next`. Parse that line; do not infer
  progress from logs. Full contract and every status value:
  `references/methodology.md`.
- `next` is a hint written with a bare `appmap` (run it through the wrapper). The step order in
  this skill wins when they differ.

**Hard rules (never break, whatever the model or harness):**

1. No run starts until the operator has explicitly confirmed authorization and scope (Step 1).
2. Never decide an approval for the human. You may recommend; only the human's answer is recorded.
3. Credentials only via environment variables (or the operator's manual login). Never ask for a
   password in chat, never put one in a file, command line or config.
4. `status: "paused_circuit"` means stop and tell the operator. Do not rerun until they say so.
5. Do not widen scope, switch to `production`, or enable forced browsing without the operator.
6. Run directories may contain production customer data. Handle them as sensitive (see
   `references/safety.md`).
7. A CAPTCHA, bot challenge or WAF block (an `observations` row of kind `captcha`, a login that
   needs one, or `rate_limited` observations) is never worked around: stop that part of the run
   and escalate to the operator (manual login, an allow-listed source, or excluding the area).

---

## Step 0 — Locate the CLI and Choose the Entry

1. Find the plugin root (the directory containing `scripts/appmap` and `AGENTS.md`) and run
   `appmap --version`. It prints `appmap 0.1.0` (after first-run setup). This command prints no
   status line.
2. Decide the entry from the input you were given:
   - a **run directory** (contains `run.json`) → resume: follow "Resuming a run" in
     `references/methodology.md`, then continue at the step it names;
   - an existing **config file** → Step 1, starting at its review of the file;
   - a **base URL** or nothing → Step 1 from the beginning.

---

## Step 1 — Confirm Authorization, Configure, Open the Run

Invoke skill: `configure-crawl`

It confirms authorization and scope with the operator (mandatory), interviews them for roles,
profile and limits, writes `appmap.config.yaml`, runs a light preflight and finally
`appmap init --config <config> --out <parent>`. Done when `init` reports
`status: "initialized"` and you have the run directory path.

---

## Step 2 — Authenticate Every Role

Invoke skill: `authenticate-roles`

Runs `appmap auth --run <run>` (per role where needed). Manual (SSO/MFA) roles are completed by
the operator in a headed browser. Done when every role has `auth_ok = 1`, or the operator has
agreed to drop a role.

---

## Step 3 — Probe Passive Sources

Invoke skill: `probe-endpoints` (passive half)

`appmap probe --run <run> --phase passive` seeds the frontier from robots.txt, sitemaps,
well-known files, OpenAPI/GraphQL schemas and JS bundles. Rerun while `in_progress`.

---

## Step 4 — Crawl

Invoke skill: `crawl-application`

`appmap crawl --run <run>` explores every role in the browser, budget by budget (default 8
minutes per invocation). Rerun while `status == "in_progress"`. Watch form failures between
invocations and add fixtures when the same forms keep failing validation. `limit_reached` ends
crawling for the run; `paused_circuit` stops everything. When the crawl reports `done` with
`pending_approvals > 0`, run the Step 6 approval review now (first checkpoint), then continue
with Step 5.

---

## Step 5 — Probe Actively

Invoke skill: `probe-endpoints` (active half)

`appmap probe --run <run> --phase active` replays captured API calls with variations, exercises
schema-only endpoints, runs forced browsing if enabled, and replays everything as every role to
fill the access matrix. Rerun while `in_progress`. Then review approvals again (second
checkpoint, Step 6) if `pending_approvals > 0`.

---

## Step 6 — Review Approvals With the Human

Invoke skill: `review-approvals` — at the two checkpoints (a crawl pass drained in Step 4; the
active probe finished in Step 5) and whenever a later pass leaves `pending_approvals > 0`.

You review the held actions, recommend (confirm, downgrade, add missed ones, with reasons),
present the batch to the human grouped by category, record **their** decisions, apply them, run
`appmap crawl --run <run> --approved-only`, then a normal `appmap crawl --run <run>` to explore
what the approved actions revealed.

---

## Step 7 — Double Back

Follow "Double back" in `references/methodology.md`:

1. `appmap status --run <run> --new-pass` — closes the pass; read `counts.new_in_prev_pass`
   (`states`, `endpoints`, `routes`) and `counts.prev_pass`.
2. **Stop** when all three numbers are zero, or `prev_pass` is 3 or more, or a crawl reported
   `limit_reached`.
3. Otherwise run `appmap status --run <run> --gaps` and read the `{"gaps": …}` line. Decide which
   gaps are actionable (better fixtures — only helps forms not yet attempted, a missed role, a newly approved action, endpoints only
   seen in bundles/schemas) and run one targeted pass (crawl, probe active, approvals). If none is
   actionable, stop.
4. Go back to 1. If it reports `status: "no_change"`, nothing ran since the pass began: run the
   targeted pass first (or stop if there is none).

---

## Step 8 — Build the Map

Invoke skill: `build-app-map`

`appmap build --run <run>` derives flows and writes `appmap.json`, `APP-MAP.md`, `SCHEMA.md` and
a `names.yaml` stub. You name and describe flows and give forms a purpose in `names.yaml`,
rebuild, and review `APP-MAP.md`. Report to the operator: counts, roles, unexercised surface,
coverage gaps, records created (for cleanup), and the handling notice if the run used
production.

---

## Step 9 — Profile the Application (optional)

Invoke skill: `profile-running-application` — when the operator wants a briefing for testers or
developers, or a test-generation step follows.

`appmap profile --run <run>` (one `--run` per environment of the same application) writes
`profile.json`, `APP-PROFILE.md` and a `profile-names.yaml` stub; you fill the purpose and
feature names, descriptions and "how it works" with evidence refs (and optional confidence
downgrades), and re-run until `next` is `null`. Statuses:
`profiled`, `partial` (something could not be determined), `error`.

---

## Completion Criteria

- [ ] Operator explicitly confirmed authorization and scope; `run.json` records it.
- [ ] Every role authenticated (`auth_ok = 1`) or dropped with the operator's agreement.
- [ ] Passive probe, crawl and active probe each ended `done` (or crawl `limit_reached`).
- [ ] No `paused_circuit` left unresolved; the operator was told about any that occurred.
- [ ] Every held action was presented to the human; their decisions are in `approvals.yaml`;
      approved items were executed with `--approved-only`.
- [ ] Double-back stop rule met (zero new surface, 3 passes, or limits) — not just "it felt done".
- [ ] `appmap build` completed after `names.yaml` was filled in; `APP-MAP.md` reviewed.
- [ ] Final report given to the operator, including the data-handling notice for production runs.

---

## Reference Files

- `references/methodology.md` — APPMAP_STATUS contract and every status value, the loop, budgets
  and reruns, the double-back procedure and stop rule, resuming a run.
- `references/config-reference.md` — every `appmap.config.yaml` key, defaults, profiles, login
  types, examples.
- `../profile-running-application/SKILL.md` — the optional application profile (Step 9).
- `references/safety.md` — authorization, scope, throttling and circuit breaker, approvals,
  redaction, credentials, sensitive-data handling and cleanup.
