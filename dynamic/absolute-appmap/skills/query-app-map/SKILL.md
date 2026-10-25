---
name: query-app-map
description: Use when you need facts about a web application from an existing absolute-appmap run directory (appmap-<host>-<timestamp>/ with appmap.sqlite, appmap.json and APP-MAP.md) - e.g. to generate Playwright end-to-end or HTTP/API integration tests, plan authorization testing, or answer questions about routes, endpoints, forms, flows, locators, request/response shapes and which roles can reach what - without re-crawling. Covers orientation, appmap query recipes, read-only SQL, and drilling into events.jsonl and HAR for evidence. Triggers on "use the app map", "query the appmap run", "which endpoints can the user role reach", "how do I reach this page", "what does this endpoint return", "write tests from the app map".
---

## Purpose

A finished run directory is a self-describing, shareable map of the application. This skill tells
any downstream agent — including a test-generation agent — how to answer questions from it
precisely, cheaply, and with evidence, in this order: orientation files → query recipes → SQL →
raw evidence.

**Handling notice:** run directories can contain real customer data (full request/response
bodies, screenshots, DOM snapshots), especially when `run.json` has
`contains_production_data: true`. Credentials and session tokens are redacted (`<redacted:KIND>`);
nothing else is. Quote only what the task needs, prefer patterns and counts over raw bodies, and
never copy run-directory content into source repositories, tickets or chats beyond what the
operator asks for.

`appmap` means the plugin's `scripts/appmap` wrapper (run by absolute path); every command ends
with an `APPMAP_STATUS {json}` line (contract in
`../map-running-application/references/methodology.md`). The `sqlite3` command-line tool works
too — always open the map read-only, `sqlite3 -readonly <run>/appmap.sqlite`, so nothing can
change the evidence; `appmap query` adds read-only enforcement, a statement timeout and the
recipes.

---

## Step 1 — Orient

1. **The profile first, if there is one.** `APP-PROFILE.md` (and `profile.json`) in the run
   directory — or wherever `appmap profile --out` put them — is the briefing: purpose, features,
   technology stack, layout, data model, data setup, test accounts, environments, coverage.
   Read it before anything else. If there is none and you need that context, run the
   `profile-running-application` skill (`../profile-running-application/SKILL.md`) first.
2. `run.json` — target, profile, roles, `authorization`, `schema_version`, `status`
   (`complete` = map built), `contains_production_data`.
3. `APP-MAP.md` — the human summary: route tree, endpoint inventory, role access matrix, flows,
   forms, unexercised surface, gaps.
4. `SCHEMA.md` (same content as `references/schema.md`) — every table, column and vocabulary.
   Check `meta.schema_version`; a different major version means the schema changed.

---

## Step 2 — Use a Recipe

`appmap query --run <run> <recipe> [options]` prints one JSON object per row
(`--format table` for aligned text; put `--format` before the recipe or after it).

| Recipe | Answers |
|---|---|
| `flows [--role R]` | Every flow step: UI action, label, locator set, request, status |
| `flow-form-fields --flow ID` | Fields a flow submits, with locators and the body sent |
| `forms-posting-to --endpoint P` | Forms whose submission hit an endpoint (exact pattern or LIKE, e.g. `/api/%`) |
| `endpoint-shape --method M --path P` | Parameters, then per-role response shape, statuses, sample exchange ids |
| `path-to-state --state ID` | Step 0 URL to load, then click/submit steps with ranked locators to reach a state |
| `hidden-but-reachable --role R` | Items the role may access (cross-role replay) but never reached in its own UI |
| `role-matrix` | The whole access matrix |
| `gaps` | Coverage gaps (as `appmap status --gaps`) |

Examples: `appmap query --run <run> flows --role admin`,
`appmap query --run <run> endpoint-shape --method GET --path '/api/projects/{id}'`.

---

## Step 3 — Write SQL When No Recipe Fits

`appmap query --run <run> sql "SELECT method, path_pattern, kind FROM endpoints WHERE kind != 'static' ORDER BY path_pattern"`

- One `SELECT`, `WITH` or `EXPLAIN QUERY PLAN` statement; anything that writes, attaches, or runs
  `PRAGMA` is refused (including `pragma_table_info()`: read columns from `SCHEMA.md`);
  10-second statement timeout.
- At most 1000 rows are printed by default; `counts.truncated` in `APPMAP_STATUS` says how many
  were left out. Narrow the query (`WHERE`, `LIMIT`/`OFFSET`) or pass `--limit N` (`0` = all).
- JSON columns (`TEXT JSON` in the schema) work with `json_extract` / `json_each`.
- `frontier` and `frontier_links` are crawler-internal; do not build on them.
- Ready-made queries for common tasks: `references/example-queries.md`.

---

## Step 4 — Drill Into Evidence Only When Needed

- `exchanges` rows hold full redacted requests and responses; bodies over 256 KiB or binary are
  files under `bodies/` (`req_body_path`, `resp_body_path`, relative to the run directory).
- `*_event` / `event_id` columns point into `events.jsonl` (one JSON object per line: `id`, `ts`,
  `type`, `role`, `caused_by`, `data`); follow `caused_by` to reconstruct what triggered what.
- `traffic/<role>.har` — standard HAR per role for tools that read HAR.
- `artifacts/<state id>.png` / `.html` — screenshot and DOM snapshot per state.

---

## Step 5 — Use Results for Tests

- **Browser E2E:** `path-to-state` and `flows` give the URL to load and the locators per step;
  try locators in ascending `rank` (`testid`, `role_name` → `get_by_role(role, name=…)`,
  `label`, `text`, `css`, `xpath`). Steps marked `mutating` change server state.
- **HTTP/API:** `endpoint-shape` for parameters and response shapes; `value_links` and flow
  `preconditions.requires` say which earlier response produces a value a request needs.
- **Authorization tests:** `role-matrix` and `hidden-but-reachable`; `different_content` and
  `not_tested` need human interpretation.
- Never point generated tests at production unless the operator says so; prefer the
  environment the run mapped.

---

## Completion Criteria

- [ ] Oriented from `APP-PROFILE.md` (when present), `run.json`, `APP-MAP.md` and the schema
      version before querying.
- [ ] Answers come from recipes or read-only SQL, with ids (exchange, state, flow) as evidence.
- [ ] Raw bodies and personal data quoted only as far as the task requires.

---

## Reference Files

- `references/schema.md` — tables, columns, vocabularies and recipe SQL (identical to
  `SCHEMA.md` in every run directory).
- `references/example-queries.md` — SQL for common questions and test-generation tasks.
- `../profile-running-application/SKILL.md` — producing `APP-PROFILE.md` / `profile.json`.
