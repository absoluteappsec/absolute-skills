# Design Spec: `absolute-appmap` Plugin

**Date:** 2026-10-24
**Author:** Seth Law (Redpoint Security / Absolute AppSec)
**Status:** Approved for planning
**Location:** `absolute-skills/dynamic/absolute-appmap/`

## Summary

`absolute-appmap` is a skill plugin that drives a coding agent to **comprehensively crawl a running
web application** — traditional server-rendered apps and SPAs alike — using Playwright plus direct
HTTP requests. It exercises every reachable link, route, client-side state, and form, for every
configured user role, and records every interaction in a shareable, queryable format. On
completion it emits a complete **application map**.

It is the first plugin in the `dynamic/` family of `absolute-skills` (skills that review *running*
applications), and the dynamic counterpart to `absolute-codereview`'s static `map-application`.

## Intent and the Why

The primary consumer is a **future test-generation skill** that will write unit and integration
tests for the target application. That skill needs the *shape of the whole application*: not just
a list of URLs, but the flows a user moves through (page → action → API call → resulting state),
which roles can reach what, what each form accepts, and what each endpoint returns. The map must be
precise enough to derive test flows from and structured enough that future agents can query it
without re-crawling or reading raw traffic.

**Test types the consumer writes, in priority order:** (1) browser end-to-end tests (Playwright)
against the running app; (2) HTTP/API integration tests; (3) unit tests inside the app's codebase
when source access is available. Consequences for this plugin: every UI action stores replayable,
multi-strategy locators; every request/response pair is queryable with full bodies (not only in
HAR); flows record value provenance between steps and preconditions; and endpoints/routes carry
enough identifying detail (paths, operation names, handler hints from responses/source maps) for
a later skill to correlate them with source code.

Secondary consumers: security testers (attack surface for authorization/injection testing) and
humans who want a readable map of an unfamiliar app.

## Goals

- Discover the **entire reachable surface** of a running web app: pages, client-side routes/states,
  links, clickable controls, forms, API endpoints (REST, GraphQL operations, WebSocket/SSE), and
  their parameters.
- **Exercise** it: follow every in-scope link, trigger every clickable control, and **submit every
  form** — with destructive actions held for human approval.
- Crawl as **multiple roles**, each with its own credentials, and produce a role × surface access
  matrix.
- First-class **SPA support** (see "SPA Layer").
- Go **beyond the UI** with passive sources and direct requests (see "Discovery Sources").
- Record **every interaction** in open, documented, tool-agnostic formats (JSONL, HAR, SQLite,
  JSON, Markdown).
- Emit a **complete application map** at the end, plus a coverage-gaps report.
- Be **portable**: usable by any skill-aware coding agent or harness (Claude Code, Codex, Gemini CLI,
  Cursor, etc.) and across models. No dependence on MCP servers or vendor-specific tools.
- Be **safe to run** against staging by default and against production with throttling.

## Non-Goals (v1)

- Vulnerability detection, fuzzing for bugs, or exploit attempts. This plugin maps; other skills
  test. (Recording anomalies like stack traces or 500s as *observations* is fine.)
- Generating tests. That is the future sibling skill's job; this plugin only produces its input.
- Native mobile apps, desktop apps, or non-HTTP protocols.
- Defeating CAPTCHAs or bot protection. If encountered, the crawler records it and asks the human.
- A GUI or dashboard. `APP-MAP.md` is the human view.
- Static/source-code route discovery (that is `absolute-codereview:map-application`). A later
  reconciliation skill may diff the two maps; v1 only stores enough to make that possible.

## Anti-Goals (failure even if it "works")

- Silently performing a destructive action (delete, password/email change, payment, outbound
  message, account deactivation, logout) without approval.
- Leaking credentials, session cookies, or tokens into any output file.
- Hammering a production system, or ignoring scope and wandering onto third-party domains.
- An output only the crawler can read: undocumented formats, opaque blobs, or a schema that
  requires the original code to interpret.
- A skill that only works with one agent, one model, or one MCP server.
- Endless crawling of near-duplicate SPA states or infinite lists, presented as "coverage."

## Constraints

- **Environments:** mostly staging; sometimes production with throttling.
- **Authorization:** used only against applications the operator is authorized to test. The
  orchestrator confirms authorization and scope at the start of every run and records it in the
  run metadata.
- **Runtime:** Python 3.11+ with Playwright for Python (Chromium by default). Dependencies installed
  into a local virtualenv by a setup script; nothing global.
- **Agent-agnostic:** SKILL.md files follow the open Agent Skills format (frontmatter `name` +
  `description`, markdown body, `references/` and `scripts/` alongside). Instructions refer to
  generic capabilities ("run this command", "read this file"), never harness-specific tool names.
  All heavy lifting is in CLI scripts any agent can run from a shell. A top-level `AGENTS.md` points
  non-Claude harnesses at the skills.
- **Conventions:** mirror `absolute-codereview` (see "Plugin Structure").

## Decisions Made in Conversation

These came from the user and are fixed:

1. Works for both standard (server-rendered) and SPA applications, with a dedicated SPA layer.
2. Output feeds a future unit/integration test-generation skill; must be referenceable and
   queryable by future agents.
3. **Submit all forms**, but **flag destructive ones for approval first**.
4. **Multiple roles**, credentials provided per role.
5. Follow `absolute-codereview` conventions; this is the **first plugin** in `absolute-skills`,
   placed under **`dynamic/`** (it reviews running applications).
6. Environments: **mostly staging, sometimes production with throttling**.
7. "Direct requests" means the **broad** version: robots/sitemaps, OpenAPI/GraphQL schemas, JS
   bundle mining, and replaying discovered API calls with varied parameters.
8. The SPA layer as proposed below was accepted.
9. Consumer test types: browser E2E first, HTTP/API integration second, in-codebase unit tests
   when source is available.
10. **Cross-role replays of POST/PUT/PATCH do not require approval.** Destructive actions (per the
    destructive heuristics, including all `DELETE`s) still do.
11. **Keep everything in production runs** — full HAR bodies, screenshots, DOM snapshots, example
    values. No PII stripping. Production runs are limited in number; the data is needed for future
    reference. (Credential/session-token redaction still applies.)

Calls Claude made (reviewable — see the end of this doc for the full list): plugin name, Python as
runtime, output layout, SQLite as the query index, config format, approval mechanics, the
production profile's defaults, and the skill decomposition.

## Plugin Structure

```
absolute-skills/
└── dynamic/
    └── absolute-appmap/
        ├── .claude-plugin/
        │   └── plugin.json
        ├── AGENTS.md                      # entry point for non-Claude harnesses
        ├── README.md
        ├── LICENSE                        # MIT, as absolute-codereview
        ├── commands/
        │   ├── appmap.md                  # start a new mapping run
        │   └── appmap-resume.md           # resume an interrupted/paused run
        ├── scripts/
        │   ├── setup.sh                   # create venv, install playwright + browsers
        │   └── validate.py                # structural validator (same checks as codereview)
        ├── docs/
        │   ├── usage.md
        │   └── superpowers/specs/         # this document
        └── skills/
            ├── map-running-application/   # ORCHESTRATOR
            │   ├── SKILL.md
            │   └── references/
            │       ├── methodology.md     # the crawl loop, stop criteria, double-back
            │       ├── config-reference.md
            │       └── safety.md          # scope, throttling, approvals, redaction
            ├── configure-crawl/           # phase: scope, roles, profile, preflight
            ├── authenticate-roles/        # phase: per-role login + session capture
            ├── crawl-application/         # phase: browser crawl incl. SPA layer
            │   └── references/spa-techniques.md, form-filling.md
            ├── probe-endpoints/           # phase: passive sources + direct requests
            │   └── references/discovery-sources.md
            ├── review-approvals/          # phase: present/decide the destructive-action queue
            │   └── references/destructive-heuristics.md
            ├── build-app-map/             # phase: synthesize flows, emit map + gaps
            │   └── references/app-map-template.md
            └── query-app-map/             # CONSUMER skill for downstream agents
                └── references/schema.md, example-queries.md
        └── lib/appmap/                    # the Python package the scripts expose as a CLI
```

The Python engine lives in a single package (`lib/appmap/`) exposed as one CLI, `appmap`, with
subcommands that the skills invoke (`appmap init`, `appmap auth`, `appmap crawl`, `appmap probe`,
`appmap approvals`, `appmap build`, `appmap status`, `appmap query`). Skills never contain inline
crawler code; they tell the agent which subcommand to run, how to read its output, and where to
apply judgment.

SKILL.md conventions (from `absolute-codereview`): frontmatter `name` equals the directory name;
`description` ≤ 1024 chars, starts with "Use when…/Use during…", includes trigger phrases; body is
Purpose → `## Step N — …` sections → `## Completion Criteria` checklist → `## Reference Files`.
Detailed material lives in `references/` and loads on demand. `validate.py` extends the codereview
validator to also check that every `scripts/` and CLI subcommand referenced in a SKILL.md exists.

### Commands

- `/appmap <config.yaml | base URL>` — start a run via the orchestrator. Given a bare URL, the
  orchestrator walks the operator through `configure-crawl` to produce a config.
- `/appmap-resume <run-dir>` — resume a paused or interrupted run.

## Division of Labor: Scripts vs. Agent Judgment

Mechanical, high-volume work belongs in scripts (deterministic, cheap, model-independent). Judgment
belongs to the agent. This keeps the skill reliable across weaker and stronger models.

| Scripts do | The agent does |
|---|---|
| Drive the browser, enumerate clickables, detect SPA routers, fingerprint states | Build the config with the operator; confirm authorization and scope |
| Fill and submit forms with generated data; retry on validation errors | Supply realistic domain data where generated data keeps failing (writes a fixtures file) |
| Throttle, enforce scope, redact secrets, write all output | Review the destructive-action queue and present it to the human with reasons |
| Heuristically flag destructive actions | Confirm, downgrade, or add flags the heuristics missed |
| Mine bundles/schemas, replay requests, normalize endpoints | Name and describe flows; read the coverage-gaps report and decide on targeted extra passes |
| Emit structured status after every subcommand (JSON on stdout) | Decide when coverage is good enough; write the narrative parts of `APP-MAP.md` |

Every CLI subcommand prints a final machine-readable JSON status line (counts, pending approvals,
errors, next suggested action), so any agent can follow progress without parsing logs.

## Run Configuration

One YAML file per run (`appmap.config.yaml`). Example shape:

```yaml
target:
  base_urls: ["https://staging.example.com"]
  scope:
    allow_hosts: ["staging.example.com", "api.staging.example.com"]
    allow_path_prefixes: ["/"]
    deny_patterns: ["/admin/billing/*", "*/logout"]
authorization:
  confirmed_by: "operator name"     # required; orchestrator asks
  notes: "Engagement #1234, staging only"
profile: staging                    # staging | production
limits:
  max_requests_per_second: 5        # production default: 1, with jitter
  concurrency: 4                    # production forced to 1
  max_states_per_route_pattern: 5   # SPA dedup / list sampling
  max_runtime_minutes: 240
  max_depth: 15
roles:
  - name: admin
    login: { type: form, url: /login, username_env: APPMAP_ADMIN_USER, password_env: APPMAP_ADMIN_PASS }
  - name: user
    login: { type: form, url: /login, username_env: APPMAP_USER_USER, password_env: APPMAP_USER_PASS }
  - name: anonymous
    login: { type: none }
  - name: sso-user
    login: { type: manual }         # headed browser, operator logs in once, session saved
discovery:
  passive_sources: true             # robots, sitemap, .well-known, source maps
  schemas: true                     # OpenAPI/Swagger, GraphQL introspection
  bundle_mining: true
  api_replay: true
  forced_browsing: false            # wordlist; blocked in production profile
form_data:
  marker_prefix: "appmap-{run_id}"  # tags created data for later cleanup
  fixtures: ./fixtures.yaml         # optional domain-specific values
```

Credentials are referenced via environment variables or a separate, git-ignored secrets file —
never inline, never written to output.

## Environment Profiles

| | `staging` (default) | `production` |
|---|---|---|
| Rate | configurable, default 5 rps | default 1 rps + jitter |
| Concurrency | configurable | forced to 1 |
| Destructive actions | approval queue | approval queue |
| Data-creating / notification-sending actions | submitted | **also** require approval |
| Forced browsing | allowed if enabled | blocked |
| Circuit breaker | pause on sustained 429/5xx | pause sooner, lower threshold |

In both profiles: scope allow/deny lists are enforced on browser **and** direct requests;
out-of-scope URLs are recorded (as `external_links`) but never requested; all created data carries
the marker prefix so it can be found and cleaned up.

## Multi-Role Crawling

- Each role gets an isolated browser context and session.
- Login types: `form` (scripted login from env-var credentials), `manual` (headed browser, operator
  completes SSO/MFA once, session state saved), `none` (anonymous), and `storage_state` (operator
  supplies a pre-captured Playwright storage-state file).
- Session capture includes cookies, `localStorage`, and `sessionStorage` (SPAs often keep tokens in
  web storage).
- **Session-loss detection:** redirects to the login URL, 401/403 spikes, or a login form appearing
  → re-authenticate and retry the step. Logout, login, and password-reset flows are *recorded* but
  never used to switch identity mid-crawl. Logout controls are always held.
- **Cross-role replay:** after each role crawls, every route and endpoint discovered by any role is
  directly requested as every other role. The result populates the `role_access` matrix with
  observed outcomes (200 / redirect-to-login / 403 / 404 / different content), distinguishing
  "hidden in the UI" from "actually blocked." Cross-role replays of POST/PUT/PATCH are sent
  without approval (decision 10); destructive actions, including any `DELETE`, remain held.

## Discovery Sources

All sources feed one de-duplicated frontier, and every request — browser or direct — passes through
the same scope, throttle, redaction, and approval gates.

1. **Browser crawl:** anchors, clickable controls, client-side route changes, revealed menus,
   modals, tabs, and forms (see SPA layer).
2. **Passive sources:** `robots.txt`, `sitemap.xml` (including indexes), `/.well-known/*`,
   `security.txt`, `manifest.json`, source maps when exposed.
3. **Schemas:** OpenAPI/Swagger at common paths and any linked from pages; GraphQL introspection
   when enabled on the server.
4. **JS bundle mining:** API path strings, route tables, `fetch`/`axios`/`XMLHttpRequest` call
   sites, base URLs, and GraphQL operation documents.
5. **API replay:** captured API calls are replayed with varied methods (OPTIONS/HEAD and safe
   alternatives), parameter variations (omitted optional params, pagination, sort/filter values
   seen elsewhere), and IDs observed in other responses. Endpoints found only in schemas or bundles
   are exercised directly with schema-derived or inferred parameters. State-changing replays obey
   approval rules.
6. **Forced browsing (optional):** a small built-in wordlist plus operator-supplied lists. Off by
   default, blocked in production.

Every discovered item records **which source(s)** found it, so the map can distinguish
"linked in UI" from "only in bundle" from "only in schema."

## SPA Layer

Activates automatically when a client-side router or SPA framework is detected (also forceable via
config).

- **Route extraction from the framework runtime:** React Router, Vue Router, Angular Router, Next.js
  (`__NEXT_DATA__`, `_buildManifest.js`), Nuxt, SvelteKit manifests; fallback to route patterns
  mined from bundles and source maps. Every extracted route is navigated directly for every role.
- **Clickable discovery beyond anchors:** CDP-based enumeration of elements with click listeners;
  ARIA roles (`button`, `link`, `tab`, `menuitem`, `option`); `cursor:pointer`; `[onclick]`.
  Expands menus, tabs, accordions, dropdowns, and hover-revealed controls to surface hidden actions.
- **State graph, not URL list:** a state is identified by URL (path + normalized query/hash) plus a
  structural DOM fingerprint (tag/role/landmark skeleton with text, timestamps, IDs, and counts
  stripped). Transitions record the triggering action and resulting network calls.
- **State restoration by replay:** to explore from an earlier state, the crawler replays the
  recorded action path from a known entry point, rather than trusting browser Back.
- **Settledness:** an action is "done" when the network is idle *and* DOM mutations have stopped for
  a configurable quiet window — not on `load`.
- **Non-`<form>` forms:** clusters of inputs plus a submit-like control are treated as forms.
  Controlled inputs (React/Vue) are filled with real keystroke events. Handlers for common custom
  widgets: date pickers, comboboxes/autocompletes, multi-selects, rich-text editors, file uploads.
- **API surface:** GraphQL keyed by operation name (and type: query/mutation/subscription);
  WebSocket and SSE messages recorded; tokens from web storage captured per role.
- **Service workers disabled** so cached responses don't hide traffic.
- **Infinite scroll / pagination / virtualized lists:** sample a bounded number of pages/items per
  route pattern (`max_states_per_route_pattern`), then stop.
- **Dialogs:** `alert`/`confirm`/`prompt` are recorded; `confirm` dialogs are a destructive-action
  signal.

## Forms

- Every discovered form (real or inferred) is recorded with its fields, types, constraints
  (`required`, `pattern`, `min/max`, `maxlength`, options), method, and action/target endpoint.
- **Data generation:** type- and name-aware values (email, phone, dates, numbers in range, enum
  options), tagged with the marker prefix where the field is free text. Operator fixtures override.
- **Validation loop:** submit; if the app returns validation errors (inline messages, 4xx with
  field errors), adjust and retry up to N times. Record both the failing and succeeding
  submissions — the validation behavior is itself valuable to the test-generation consumer.
- **File uploads:** small benign sample files per common type (txt, png, pdf, csv).
- **Multi-step forms/wizards** are followed as flows, each step recorded.

## Destructive-Action Approval

- **Heuristic flags:** HTTP `DELETE`; controls/URLs/labels matching delete, remove, destroy, cancel,
  deactivate, disable, revoke, transfer, pay, purchase, refund, send, invite, reset, change
  password/email, logout/sign out; `confirm()` dialogs or "are you sure" modals; forms posting to
  endpoints flagged previously. Production adds: anything creating data or sending notifications.
- **Queue, don't block:** flagged actions go into an approval queue and the crawl continues
  elsewhere. Each queued item records the role, state, action path to reach it, the control, the
  predicted request, and *why* it was flagged.
- **Agent review:** at checkpoints (end of each role pass, and when the frontier is exhausted) the
  `review-approvals` skill has the agent review the queue, confirm or downgrade flags with reasons,
  add any the heuristics missed, then present the batch to the human.
- **Harness-agnostic mechanics:** decisions live in `approvals.yaml` in the run directory (`approve`
  / `deny` / `skip` per item, with optional batch rules like "approve all deletes of
  `appmap-`-prefixed records"). The agent can collect answers conversationally and write the file,
  or the human can edit it directly. `appmap crawl --approved-only` then executes approved items by
  replaying their action paths.
- Denied and never-decided items appear in the final map as **known but unexercised** surface.

## Output: The Run Directory

Each run writes one self-contained directory: `appmap-<target-host>-<YYYYMMDD-HHMMSS>/`.

| File | Purpose |
|---|---|
| `run.json` | Run metadata: config (redacted), authorization confirmation, tool version, schema version, timings, profile, roles, status |
| `events.jsonl` | Append-only log of every action and observation (navigate, click, fill, submit, request, response, dialog, state-entered, approval-queued, error). Every event has an `id`, `role`, `ts`, and `caused_by` id linking it to its triggering event — the backbone for reconstructing flows |
| `traffic/<role>.har` | Full raw HTTP per role, standard HAR (secrets redacted) |
| `appmap.sqlite` | Normalized, queryable index (see Data Model) |
| `appmap.json` | Same model as a single JSON export for consumers that don't want SQL |
| `APP-MAP.md` | Human-readable map (see below) |
| `SCHEMA.md` | Copy of the schema reference with example queries, so the run dir is self-describing |
| `approvals.yaml` | Destructive-action queue and decisions |
| `artifacts/` | Screenshot + DOM snapshot per unique state, keyed by state id |
| `crawl.log` | Human-oriented log |

The run directory is the shareable unit: zip it and hand it to another agent or person. Paths inside
are relative.

### Data Model (`appmap.sqlite`)

Tables (columns abbreviated; full definitions in `query-app-map/references/schema.md`):

- `meta` — `schema_version`, run id, target, tool version.
- `roles` — name, login type, auth outcome.
- `states` — id, url, route_pattern, dom_fingerprint, title, first_seen_event, screenshot path.
- `transitions` — from_state, to_state, role, action (type, selector, label), event id.
- `routes` — client and server route patterns (e.g. `/users/{id}`), kind (`page`, `spa_route`),
  sources.
- `endpoints` — method, normalized path, host, kind (`rest`, `graphql_op`, `websocket`, `sse`,
  `static`), graphql operation name/type, content types, sources.
- `params` — endpoint, location (path/query/body/header/cookie), name, observed types, example values
  (redacted where sensitive), required-ness evidence.
- `exchanges` — every request/response pair: endpoint, role, event id, full request (method, URL,
  headers, body) and full response (status, headers, body), with credential redaction only. Large
  or binary bodies are stored as files under `bodies/` and referenced by path.
- `responses` — per endpoint × role summary: statuses seen, content types, body shape (JSON key
  skeleton), size range, representative exchange ids.
- `locators` — per action target: ARIA role + accessible name, test id, label, text, CSS and XPath
  fallbacks, with a stability ranking.
- `flow_steps` / `value_links` — ordered steps per flow, and provenance links (value produced by
  exchange A, consumed by exchange B) so tests can chain steps; `flows` carry preconditions.
- `forms` — state, inferred/real, method, action endpoint, purpose label (agent-written).
- `fields` — form, name, type, constraints, options, values tried, validation messages seen.
- `flows` — named, ordered sequences of transitions/requests (e.g. "create project", "invite member"),
  with role and agent-written description. Derived from `caused_by` chains plus agent naming.
- `role_access` — (route or endpoint) × role → observed outcome and evidence event id.
- `sources` — item → discovery source(s) (`ui`, `spa_router`, `bundle`, `openapi`, `graphql`,
  `sitemap`, `robots`, `replay`, `wordlist`).
- `approvals` — queued actions, reason, decision, decided_by, executed event id.
- `observations` — notable non-findings: 5xx, stack traces, debug pages, CAPTCHA, rate limiting.
- `external_links` — out-of-scope URLs seen and where.

Every table includes enough foreign keys back to `events.jsonl` ids that a consumer can drill from
summary to raw evidence. The schema is **versioned** (`meta.schema_version`, semver); breaking
changes bump the major version and are documented in `schema.md`.

### `APP-MAP.md`

Sections: run summary (target, roles, profile, duration, counts); route tree; endpoint inventory
grouped by resource; role access matrix; key flows with step lists; forms inventory with purpose
and fields; GraphQL operations; pending/denied approvals (known-but-unexercised surface); coverage
gaps; observations; pointers to `appmap.sqlite`/`SCHEMA.md` for detail.

## Orchestrator Flow (`map-running-application`)

Mirrors the Circle-K structure of `absolute-codereview`:

0. **Open the run.** Run `appmap init`; record authorization confirmation and scope.
1. **Configure** → `configure-crawl`: build/validate config with the operator; preflight
   (reachability, scope sanity, robots/rate-limit headers, framework/SPA detection).
2. **Authenticate** → `authenticate-roles`: log in each role, verify, save sessions.
3. **Probe (passive)** → `probe-endpoints` passive half: seed the frontier from robots, sitemaps,
   schemas, bundles.
4. **Crawl** → `crawl-application`: per role, browser crawl with SPA layer until frontier exhausted
   or limits hit.
5. **Probe (active)** → `probe-endpoints` active half: API replay, schema-only endpoints,
   cross-role replay.
6. **Approve** → `review-approvals`: review and present the queue; execute approved actions; any
   new surface they reveal goes back into the frontier.
7. **Double back:** read the coverage-gaps report (`appmap status --gaps`): endpoints seen in
   bundles/schemas but never exercised, forms that never submitted successfully, routes reached by
   no role, states with unexplored controls. If gaps are actionable (e.g. better fixtures, a missed
   role, a newly approved action), run a targeted pass. **Stop** when a pass adds no new
   states/endpoints, or after 3 passes, or when limits are hit.
8. **Build the map** → `build-app-map`: finalize SQLite/JSON, have the agent name and describe flows
   and form purposes, write `APP-MAP.md`.

Runs are **resumable**: all state lives in the run directory; `appmap crawl --resume` picks up the
frontier where it left off.

## Consumer Skill: `query-app-map`

Teaches any downstream agent (including the future test-generation skill) how to use a run
directory: read `run.json` and `APP-MAP.md` for orientation, then answer specific questions via
`sqlite3 appmap.sqlite` (or `appmap query`, which wraps common queries) and drill into
`events.jsonl`/HAR only for evidence. `references/example-queries.md` covers recipes such as: all
flows for role X; every form posting to an endpoint; endpoints reachable by `user` but only linked
for `admin`; request/response shape for an endpoint; the action path to reach a state.

## Portability

- Skills are plain markdown in the open Agent Skills format; the plugin manifest is for Claude Code
  discovery, and `AGENTS.md` gives other harnesses a short entry point listing the skills and the
  `appmap` CLI.
- No reliance on Playwright MCP or any MCP server. If a harness has a browser MCP, skills may note it
  as an optional aid for operator-assisted steps (e.g. manual login), never as a requirement.
- All agent-facing outputs are JSON status lines or files in open formats, so weaker models can follow
  progress by reading structured fields rather than interpreting logs.
- Instructions favor explicit commands and checklists over open-ended reasoning, and isolate the
  judgment steps (approvals, fixtures, flow naming, gap review) so they can be done well by any
  capable model.

## Safety and Redaction

**Sensitive output.** Per decision 11, run directories — especially production ones — contain real
application data, including customer PII. `run.json` records a `contains_production_data` flag, and
`README.md`/`APP-MAP.md` carry a handling notice. Treat run directories as sensitive engagement data.

- Authorization and scope confirmation required to start; recorded in `run.json`.
- Redaction on by default in every output: passwords, `Authorization` headers, session cookies,
  bearer/JWT tokens, API keys, CSRF tokens (value replaced, presence kept), plus operator-configured
  patterns. Redaction preserves shape (e.g. `<redacted:jwt>`) so consumers still know a token was
  there.
- Throttle, scope, circuit breaker, and approval gates apply uniformly to browser and direct
  requests.
- The marker prefix on created data supports post-run cleanup; `APP-MAP.md` lists records the run
  created where identifiable.

## Testing the Plugin

- `scripts/validate.py` structural checks (as codereview, plus script/subcommand references).
- Engine tests against **local fixture apps** shipped in the repo: a small server-rendered app
  (forms, auth, roles, a destructive action), and a small SPA (client router, non-`<form>` forms,
  controlled inputs, infinite list, GraphQL endpoint, token in localStorage). Assertions are on the
  produced `appmap.sqlite` (expected routes, endpoints, forms, role access, approvals queued).
- Skill behavior checked by running the orchestrator end-to-end against the fixture apps with at
  least two different agents/models.

## Left to the Builder

- Internal architecture of the `lib/appmap` engine (module layout, frontier/scheduler design,
  async model).
- Exact DOM-fingerprint algorithm and quiet-window defaults, within the intent above.
- Exact heuristic word lists and scoring for destructive-action flagging.
- Choice of fixture apps' stack.
- `appmap query` convenience subcommands beyond the recipes listed.
- Exact columns and indexes in SQLite, within the table list above.

## Calls Claude Made (for review)

1. Plugin name `absolute-appmap`; path `absolute-skills/dynamic/absolute-appmap/`.
2. Python + Playwright for Python as the engine, exposed as a single `appmap` CLI.
3. Skill decomposition: orchestrator + six phase skills + a `query-app-map` consumer skill;
   commands `/appmap` and `/appmap-resume`.
4. Output layout: run directory with `events.jsonl`, per-role HAR, `appmap.sqlite`, `appmap.json`,
   `APP-MAP.md`, `SCHEMA.md`, `approvals.yaml`, `artifacts/`.
5. SQLite as the primary query index.
6. Approvals are queued (crawl continues) and decided at checkpoints via `approvals.yaml`.
7. Production profile: 1 rps + jitter, concurrency 1, data-creating actions also need approval,
   forced browsing blocked.
8. Forced browsing optional and off by default.
9. Redaction on by default with shape-preserving placeholders.
10. Convergence guard: stop after a no-new-surface pass or 3 passes.
11. Cross-role replay of all discovered surface to build the access matrix.
12. Replayable locators: `transitions` store several locator strategies per action (ARIA role +
    accessible name, test id, label/text, CSS fallback), not a single selector.
13. Data dependencies: `flows` record value provenance between steps (e.g. an ID returned in step 2
    and used in step 4), and preconditions (role, required existing records).
14. Publishing: `absolute-skills/` gets a top-level `.claude-plugin/marketplace.json` listing
    plugins by relative path (`dynamic/absolute-appmap`). Whether `absolute-codereview` moves in
    is out of scope for this spec; `validate.py` is copied for now.
15. Install location: `setup.sh` creates the virtualenv in a per-user cache
    (`${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv`) because plugin directories are
    replaced on update; Playwright browsers use Playwright's default per-user cache. Skills locate
    the CLI via `scripts/appmap` (a thin wrapper that runs setup if the venv is missing).
16. Saved sessions (storage state) live in the per-user cache keyed by run id, never inside the
    shareable run directory.
