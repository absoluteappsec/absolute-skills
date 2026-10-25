# absolute-appmap

A skill plugin that drives a coding agent to **comprehensively crawl a running web application**
— server-rendered apps and SPAs alike — and emit a complete, queryable **application map**. It is
the first plugin in the `dynamic/` family of [Absolute AppSec](https://absoluteappsec.com)'s
`absolute-skills` (skills that work against running applications) and the dynamic counterpart of
`absolute-codereview`'s static `map-application`.

## Philosophy: Scripts Do the Volume, the Agent Does the Judgment

Crawling, form filling, SPA route extraction, throttling, scope enforcement, redaction and
recording are deterministic work done by one CLI, `appmap` (Python + Playwright). The agent runs
it, reads a machine-readable status line after every command, and owns the judgment calls:
confirming authorization and scope with the operator, supplying realistic data when generated
data keeps failing, reviewing destructive actions and presenting them to the human, naming flows,
and deciding when coverage is good enough. Destructive actions are never performed without a
human's approval.

The loop mirrors the Circle-K structure of `absolute-codereview`: configure → authenticate →
probe (passive) → crawl → probe (active) → approve → double back (stop after a pass adds nothing
new, after three passes, or at the limits) → build the map.

It works with any skill-aware agent (Claude Code, Codex, Gemini CLI, Cursor, …): skills are plain
Markdown, the engine is a shell command, and there is no MCP dependency. Non-Claude harnesses
start at [`AGENTS.md`](AGENTS.md).

## Requirements

- Python 3.11+ (3.11–3.13 recommended; the setup script prefers 3.12/3.11 when the default
  `python3` is 3.14 or newer, refuses anything older than 3.11, and honours
  `APPMAP_PYTHON=/path/to/python`).
- Network access on first use: `scripts/appmap` runs `scripts/setup.sh`, which creates a
  virtualenv in `${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv` and installs
  Playwright's Chromium.
- A target you are **authorized** to test, normally staging.

## Install

### Claude Code marketplace

```
/plugin marketplace add absoluteappsec/absolute-skills
/plugin install absolute-appmap@absolute-skills
```

The repository root carries `.claude-plugin/marketplace.json`, which lists this plugin at
`./dynamic/absolute-appmap`.

### Other agents

Clone the repository and point your agent at `dynamic/absolute-appmap/AGENTS.md`.

```bash
git clone https://github.com/absoluteappsec/absolute-skills
dynamic/absolute-appmap/scripts/appmap --version   # first run installs the environment
```

## Quick Start

```
/appmap https://staging.example.com
```

The agent confirms authorization and scope with you, asks about roles and how each logs in,
writes `appmap.config.yaml` (credentials only as environment-variable names), and runs the loop.
You will be asked to set credential environment variables, to log in by hand for SSO/MFA roles,
and to decide on held destructive actions. Resume an interrupted run with:

```
/appmap-resume ./appmap-staging.example.com-20261024-120000
```

The same steps by hand:

```bash
A=dynamic/absolute-appmap/scripts/appmap
$A init --config appmap.config.yaml --out ~/engagements
RUN=~/engagements/appmap-staging.example.com-20261024-120000   # the path init printed
$A auth --run "$RUN"
$A probe --run "$RUN" --phase passive
$A crawl --run "$RUN"                      # rerun while status is in_progress
$A probe --run "$RUN" --phase active
$A approvals --run "$RUN" write            # decide items in approvals.yaml, then:
$A approvals --run "$RUN" apply
$A crawl --run "$RUN" --approved-only
$A crawl --run "$RUN"                      # explore what approved actions revealed
$A status --run "$RUN" --new-pass          # double back: read new_in_prev_pass
$A status --run "$RUN" --gaps
$A build --run "$RUN"                      # name flows in names.yaml, build again
$A query --run "$RUN" role-matrix
```

[`docs/usage.md`](docs/usage.md) walks through this sequence in detail: install, configuration
and credentials, reading `APPMAP_STATUS`, budgets and resuming, approvals, output layout,
querying and safety notes.

## Commands

| Command | Description |
|---------|-------------|
| `/appmap <config.yaml \| base URL>` | Start a mapping run via the orchestrator. Given a URL (or nothing), the operator is walked through configuration first. |
| `/appmap-resume <run-dir>` | Resume a paused or interrupted run from its run directory. |

## Skills

### Orchestrator

| Skill | Description |
|-------|-------------|
| `map-running-application` | Drives the whole loop: authorization gate, phase skills in order, approval checkpoints, the double-back stop rule, final report. Entry point for both commands. |

### Phase skills

| Skill | Description |
|-------|-------------|
| `configure-crawl` | Confirms authorization and scope with the operator, interviews for roles, profile, limits and discovery options, writes `appmap.config.yaml`, preflights, runs `appmap init`. |
| `authenticate-roles` | Logs in every role (scripted form, manual SSO/MFA, storage state, anonymous), verifies `auth_ok`, troubleshoots without locking accounts. |
| `probe-endpoints` | Passive discovery (robots, sitemaps, well-known, OpenAPI/GraphQL, JS bundles, source maps) before the crawl; API replay, schema-only endpoints, forced browsing and cross-role replay after it. |
| `crawl-application` | Budgeted, resumable browser crawl for every role with the SPA layer and form filling; handles `in_progress` / `limit_reached` / `paused_circuit`; writes fixtures when forms keep failing. |
| `review-approvals` | Reviews held destructive actions, recommends with reasons, presents them to the human by category, records the human's decisions, executes approved items. |
| `build-app-map` | Derives flows and value links, writes `appmap.json` / `APP-MAP.md` / `SCHEMA.md`, has the agent name flows and form purposes, reviews the map, reports. |

### Consumer skill

| Skill | Description |
|-------|-------------|
| `query-app-map` | Teaches downstream agents (e.g. a test generator) to answer questions from a run directory: orientation, `appmap query` recipes, read-only SQL, evidence in `events.jsonl` and HAR. |

## Outputs

Each run writes one self-contained directory, `appmap-<host>-<YYYYMMDD-HHMMSS>/`:

| File | Description |
|------|-------------|
| `run.json` | Run metadata: redacted config, authorization confirmation, tool and schema version, profile, roles, status, `contains_production_data`. |
| `appmap.sqlite` | The normalized, queryable map (states, transitions, routes, endpoints, params, exchanges with full bodies, locators, forms, fields, flows, value links, role access, sources, approvals, observations). |
| `appmap.json` | The same public tables as one JSON document. |
| `APP-MAP.md` | Human-readable map: route tree, endpoint inventory, role access matrix, flows, forms, GraphQL operations, unexercised surface, coverage gaps, observations, records created. |
| `SCHEMA.md` | Schema reference with query recipes, so the directory is self-describing. |
| `events.jsonl` | Append-only log of every action and observation, linked by `caused_by`. |
| `traffic/<role>.har` | Raw HTTP per role (standard HAR, secrets redacted). |
| `approvals.yaml` | The destructive-action queue and the human's decisions. |
| `names.yaml` | Agent-written flow names/descriptions and form purposes, applied on every build. |
| `artifacts/`, `bodies/` | Screenshot + DOM snapshot per state; large or binary bodies. |
| `crawl.log` | Human-oriented log. |

Saved login sessions are kept outside the run directory, in
`~/.cache/absolute-appmap/sessions/<run_id>/`.

## Safety and Data Handling

- Runs start only after the operator confirms authorization and scope; the confirmation is
  recorded in `run.json`.
- Scope, throttling, a circuit breaker and the approval queue apply to browser and direct
  requests alike. The `production` profile caps the rate at 1 request/second with jitter, forces
  concurrency 1, holds every data-creating action for approval and refuses forced browsing.
- Credentials come only from environment variables or a manual login; passwords, session
  cookies, tokens, API keys and CSRF tokens are redacted from every output.
- **Handling notice:** apart from credentials, everything is kept — full request/response bodies,
  screenshots, DOM snapshots — including on production runs. A run directory can therefore
  contain real customer data and PII. Treat it as sensitive engagement data: keep it out of source
  control (the repository ignores `appmap-*/`), store it encrypted, share it only with people
  authorized for the engagement, and delete it (and the saved sessions) when the engagement ends.
- Created records carry the marker `appmap-<run_id>`; `APP-MAP.md` lists every record the run
  created for cleanup.

## Known Limitations

Known gaps in v0.1.0, with workarounds where one exists:

- **Failed forms are not retried within a run.** A form whose submission failed validation
  after its retries stays failed; adding values to `fixtures.yaml` only helps forms not yet
  tried. Check `appmap status --gaps` (`unsuccessful_forms`) and exercise those by hand or in a
  new run with the fixtures in place.
- **No way to add an action to the approval queue by hand.** Only actions the crawler held are
  queued. To keep the crawler away from something it does not hold, add it to
  `scope.deny_patterns`.
- **`limits.concurrency` is validated and recorded but not used:** roles are crawled one at a
  time, one page at a time.
- **Vue Router hash-mode routes** (`/#/path`) and a router `base` are not handled by route
  extraction; such routes are found only when the UI links to them.
- **Bearer/header-auth roles probe unauthenticated:** `probe --phase passive` reuses the
  cookies `appmap auth` saved, not learned `Authorization`/CSRF headers, so schemas and
  sitemaps behind header auth answer it as anonymous. The browser crawl and active probe are
  authenticated.
- **`query sql` denies `pragma_table_info()`** (and other PRAGMA functions); read the columns
  from `SCHEMA.md` instead.
- **An approved action's window is not single-shot (ruling R21):** when no write was predicted,
  every held request the acted-on page sends during the action window passes, so a page that
  polls can send it more than once.
- **Popup and cross-site iframe residuals:** a popup's very first request is refused and
  replayed once its page is armed, and redirect hops inside out-of-process (cross-site)
  iframes are not seen by the per-page redirect gate; both are scope-checked on their first
  hop only.

## Development

```bash
scripts/setup.sh --dev                      # ./.venv with dev extras
.venv/bin/pytest
python3 scripts/validate.py                  # structural checks for skills, commands, manifest
```

## Design Specification

The full design spec is at
[`docs/superpowers/specs/2026-10-24-absolute-appmap-design.md`](docs/superpowers/specs/2026-10-24-absolute-appmap-design.md).

## License

MIT License. Copyright 2026 Absolute AppSec. See [LICENSE](LICENSE).
