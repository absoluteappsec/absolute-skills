---
name: probe-endpoints
description: Use during an appmap run to discover and exercise surface beyond the UI with direct requests - the passive phase (robots.txt, sitemaps, well-known files, OpenAPI/Swagger and GraphQL introspection, JS bundle and source-map mining) before the browser crawl, and the active phase (API replay with parameter variations, schema-only endpoints, optional forced browsing, and cross-role replay that fills the role access matrix) after it. Triggers on "probe the API", "find endpoints from the bundles", "check the OpenAPI spec", "build the role access matrix", "replay as every role".
---

## Purpose

`appmap probe` finds and exercises what the browser crawl cannot see, through the same scope,
throttle, redaction and approval gates as the browser. It has two phases, run at different points
of the loop:

- `--phase passive` (orchestrator Step 3, **before** the crawl) seeds the frontier with routes
  and endpoints from robots.txt, sitemaps (incl. indexes), `/.well-known/*`, manifests, exposed
  source maps, OpenAPI/Swagger documents, GraphQL introspection and JS bundles. For React and
  Angular apps, bundle mining is the main source of client routes.
- `--phase active` (orchestrator Step 5, **after** the crawl) replays captured API calls with
  varied methods and parameters, exercises endpoints known only from schemas or bundles, runs
  forced browsing if enabled (staging only), and finally replays every route and endpoint as
  every role to populate `role_access`.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`.

**When the config has a `manual` role, run probes with stdin closed** (append `</dev/null`, e.g.
`appmap probe --run <run> --phase active </dev/null`), as for `crawl`: a lost manual session must
fail fast with a warning, never wait for keyboard input. If a command hangs after printing
`[appmap] Log in as role …`, stop it and re-authenticate that role with the operator
(`authenticate-roles`).

---

## Step 1 — Passive Phase (before the crawl)

`appmap probe --run <run> --phase passive`

- Runs as the first configured role, using its saved session (authenticated schemas and
  sitemaps); `--role <role>` picks another role.
- Which sources run is controlled by `discovery.passive_sources`, `discovery.schemas` and
  `discovery.bundle_mining` in the config.
- `counts` sums the sources: `requests`, `routes`, `queued` (frontier items added), `scripts`,
  `endpoints`, `openapi_ops`, `graphql_ops`, `external_links`.

---

## Step 2 — Active Phase (after the crawl)

`appmap probe --run <run> --phase active`

- API replay runs for every role (or only `--role <role>`); cross-role replay always covers every
  role with a working session. Requests already sent by an earlier invocation are not repeated,
  so rerunning after new surface appears only sends what is new.
- Held actions (destructive, any DELETE, and in production every write) are queued for approval,
  not sent. Cross-role POST/PUT/PATCH replays are sent (they measure access control); logout and
  approval-related endpoints are never replayed (`not_tested` in the matrix).
- `counts`: `requests`, `held`, `skipped`, `role_access`, `wordlist_found`.
- `errors` may say `role X: no saved session; run 'appmap auth --manual-login --role X' first`
  or that a saved session looks expired — run `authenticate-roles` for that role, then rerun.
- `N request(s) not replayed: captured requests hold redacted credentials` is informational.

---

## Step 3 — Loop on the Status

| `status` | Do this |
|---|---|
| `in_progress` | Budget (default 8 minutes, `--budget-minutes N`) used up. Run the same command again |
| `done` | Phase complete. Passive → `crawl-application`. Active → `review-approvals` if `pending_approvals > 0`, else the double back |
| `paused_circuit` | **Stop.** Tell the operator the target is returning 429/5xx; wait for their go-ahead (suggest a lower `max_requests_per_second`) |
| `error` | Read `errors` and the tail of `<run>/crawl.log`; fix, rerun |

---

## Step 4 — Read What It Found

After the active phase, look at the access matrix and what is still unexercised:

- `appmap query --run <run> --format table role-matrix` — outcomes per route/endpoint × role:
  `allowed`, `redirect_login`, `forbidden`, `not_found`, `different_content`, `error`,
  `not_tested`.
- `appmap query --run <run> hidden-but-reachable --role <role>` — surface a role may access but
  never reached through its own UI (key input for authorization testing).
- `appmap status --run <run> --gaps` → `unexercised_endpoints` (known only from discovery
  sources). Endpoints that stay unexercised after an active phase usually need an approval, a
  role that can reach them, or parameters only the operator can supply — note them for the double
  back rather than rerunning blindly.

Source meanings and what each phase sends: `references/discovery-sources.md`.

---

## Completion Criteria

- [ ] Passive phase run before the crawl and rerun until `done`.
- [ ] Active phase run after the crawl and rerun until `done`.
- [ ] Any `paused_circuit` reported to the operator; resumed only with their go-ahead.
- [ ] Session warnings in `errors` resolved (roles re-authenticated) and the phase rerun.
- [ ] `role_access` populated (role-matrix query returns rows) when two or more roles exist.
- [ ] `pending_approvals` handed to `review-approvals`.

Return to `map-running-application` (Step 4 after the passive phase; Step 6 after the active one).

---

## Reference Files

- `references/discovery-sources.md` — every source, what it requests, the `sources` and
  `exchanges.origin` values it produces, and the access-matrix outcomes.
- `../map-running-application/references/safety.md` — scope, throttling and approval rules that
  apply to direct requests.
