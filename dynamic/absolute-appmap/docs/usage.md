# Using absolute-appmap

This guide runs the `appmap` CLI by hand, step by step — the same loop the
`map-running-application` skill drives for an agent. Use it to understand what the agent does,
to run a mapping yourself, or to debug a run. The skills (`skills/*/SKILL.md`) remain the
authoritative procedure for agents; this page follows them.

Only map applications you are **authorized** to test. Staging is the default; production needs
the throttled `production` profile and explicit permission.

## 1. Install

Requirements: Python 3.11+ (3.11–3.13 recommended), network access on first use, macOS or Linux
with `bash`.

Always run the CLI through the wrapper `scripts/appmap` at the plugin root:

```bash
dynamic/absolute-appmap/scripts/appmap --version     # prints: appmap 0.1.0
```

On first use the wrapper runs `scripts/setup.sh`, which creates a virtualenv in
`${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv`, installs the package and
downloads Playwright's Chromium (a few minutes; progress goes to stderr). Later calls start
immediately. Run `scripts/setup.sh` directly to (re)install ahead of time.

Crawling uses **Chromium only**: browser scope enforcement relies on the Chrome DevTools
Protocol, so Firefox and WebKit are not supported.

Development install (editable, with test extras, into `./.venv`):

```bash
cd dynamic/absolute-appmap
scripts/setup.sh --dev
.venv/bin/pytest
python3 scripts/validate.py
```

The examples below use `A=/abs/path/to/absolute-appmap/scripts/appmap`.

## 2. Configure

One YAML file per run, normally `appmap.config.yaml` in your engagement directory. Keep it in
place for the whole run: every command re-reads it (its absolute path is stored in `run.json`).

```yaml
target:
  base_urls: ["https://staging.example.com"]
  scope:
    allow_hosts: ["staging.example.com", "api.staging.example.com"]
    allow_path_prefixes: ["/"]
    deny_patterns: ["*/logout", "/admin/billing/*"]
authorization:
  confirmed_by: "Jane Doe (Security Lead)"     # required: who authorized this run
  notes: "Engagement #1234, staging only"
profile: staging                               # staging | production
limits:
  max_requests_per_second: 5
  max_runtime_minutes: 240
roles:
  - name: admin
    login: { type: form, url: https://staging.example.com/login, username_env: APPMAP_ADMIN_USER, password_env: APPMAP_ADMIN_PASS }
  - name: user
    login: { type: form, url: https://staging.example.com/login, username_env: APPMAP_USER_USER, password_env: APPMAP_USER_PASS }
  - name: anonymous
    login: { type: none }
  - name: sso-user
    login: { type: manual, url: https://staging.example.com/login }
discovery:
  forced_browsing: false
form_data:
  marker_prefix: "appmap-{run_id}"
```

Notes:

- Login `url`s may be absolute or a path (`/login`, resolved against the first base URL).
- SSO through an external identity provider: put its hosts in `scope.login_allow_hosts` (used
  only by login pages). Scripts and styles served from a CDN: `scope.static_allow_hosts` (static
  subresources only). Neither is ever crawled or probed.
- Unknown keys are errors. The full key list, defaults, profiles, login types and
  `fixtures.yaml` are in `skills/map-running-application/references/config-reference.md`.
- Put logout URLs in `deny_patterns` when you can; otherwise logout links and buttons are held
  in the approval queue and never executed.

### Credentials

The config holds only environment-variable **names**; `username`/`password` keys are rejected.
Set the values in the environment of the shell that runs `appmap`, or keep them in a private
`appmap.secrets.env` that is never committed:

```bash
git check-ignore appmap.secrets.env || echo "appmap.secrets.env" >> .gitignore
touch appmap.secrets.env && chmod 600 appmap.secrets.env
# edit it yourself:  export APPMAP_ADMIN_USER=...   export APPMAP_ADMIN_PASS=...
. ./appmap.secrets.env && $A auth --run "$RUN"
```

Load the file (`. ./appmap.secrets.env &&`) before every command that logs in: `auth`, `crawl`
and `probe` (they re-login when a session expires). Never paste credentials into a chat with an
agent; the skills never ask for them.

## 3. Reading command output: `APPMAP_STATUS`

Every subcommand's **last stdout line** is `APPMAP_STATUS ` followed by one JSON object:

```
APPMAP_STATUS {"command": "crawl", "ok": true, "status": "in_progress", "counts": {"states": 41, "transitions": 77, "forms": 9, "exchanges": 612, "queued_approvals": 3}, "pending_approvals": 3, "errors": [], "next": "appmap crawl --run /work/appmap-staging.example.com-20261024-120000"}
```

- Branch on `status`, not on logs. `ok` mirrors the exit code (0/1); `errors` may be non-empty
  even when `ok` is true (warnings such as a skipped approvals rule).
- `next` suggests the following command (run it through the wrapper). The step order below wins
  when they differ.
- Lines before the status line carry data: `init` prints the run directory, `status --gaps`
  prints one `{"gaps": …}` line, `approvals list` and `query` print one JSON object per row.
- No status line and exit code 2 means the command line itself was wrong; see
  `$A <command> --help`.

A shell helper to extract it:

```bash
st() { "$A" "$@" | tee /dev/stderr | tail -n 1 | sed 's/^APPMAP_STATUS //'; }
st crawl --run "$RUN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
```

Every status value per command is listed in
`skills/map-running-application/references/methodology.md`.

## 4. The command sequence

```bash
$A init --config "$PWD/appmap.config.yaml" --out ~/engagements
RUN=~/engagements/appmap-staging.example.com-20261024-120000     # the path init printed

$A auth --run "$RUN"                                  # every role; status authenticated
$A auth --run "$RUN" --role sso-user                  # manual roles: headed browser, press Enter
$A probe --run "$RUN" --phase passive                 # robots, sitemaps, schemas, bundles
$A crawl --run "$RUN"                                 # rerun while status is in_progress
$A probe --run "$RUN" --phase active                  # API replay + cross-role access matrix
$A approvals --run "$RUN" write                       # then record decisions in approvals.yaml
$A approvals --run "$RUN" apply
$A crawl --run "$RUN" --approved-only                 # executes approved actions only
$A crawl --run "$RUN"                                 # explores what they revealed
$A status --run "$RUN" --new-pass                     # double back: read new_in_prev_pass
$A status --run "$RUN" --gaps
$A build --run "$RUN"                                 # fill names.yaml, then build again
```

| Step | Command | Leave the step when `status` is |
|---|---|---|
| Open the run | `init` | `initialized` (`config_error`: fix the file, rerun) |
| Authenticate | `auth` | `authenticated` (`auth_failed`: see `errors`, fix, rerun for that `--role`) |
| Passive probe | `probe --phase passive` | `done` (rerun while `in_progress`) |
| Crawl | `crawl` | `done` or `limit_reached` (rerun while `in_progress`; `auth_failed`: a role's login or re-login failed and it stopped with its items kept — rerun `auth` for it, then `crawl`) |
| Active probe | `probe --phase active` | `done` (rerun while `in_progress`) |
| Approvals | `approvals write` / `apply`, `crawl --approved-only`, `crawl` | no pending items you want decided; approved ones executed |
| Double back | `status --new-pass`, `status --gaps` | the stop rule below is met |
| Build | `build` | `complete` |

**Stop** whenever a command reports `paused_circuit`: the target answered with too many 429/5xx
responses. Check that it is healthy before rerunning the same command.

### Budgets and resuming

`crawl` and `probe` stop cleanly after `--budget-minutes` (default 8) and report `in_progress`;
rerunning the identical command continues where it stopped, because all progress lives in the
run directory. Use a smaller budget if your terminal or harness limits command duration:

```bash
until [ "$(st crawl --run "$RUN" --budget-minutes 5 | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')" != in_progress ]; do :; done
```

`limits.max_runtime_minutes` caps the total browser-crawl time of the run (`limit_reached`).

To resume an interrupted run, run `$A status --run "$RUN"` and continue at the first step not
finished (the resume table in `methodology.md` maps `run.json` status and counts to a step).
Saved login sessions live in `~/.cache/absolute-appmap/sessions/<run_id>/`; if they expired,
rerun `auth` first.

### Double back (when to stop)

After each pass of work (crawl, active probe, approved execution):

1. `$A status --run "$RUN" --new-pass` closes the pass. Read `counts.prev_pass` and
   `counts.new_in_prev_pass` (`states`, `endpoints`, `routes`).
2. Stop when all three are 0, when `prev_pass` is 3 or more, or after `limit_reached`.
3. Otherwise `$A status --run "$RUN" --gaps` lists `unexercised_endpoints`,
   `unsuccessful_forms`, `unreached_routes`, `unexplored_actions`, `pending_approvals` and
   `denied_approvals`. Run one targeted pass for the actionable ones (fixtures for forms not yet
   attempted, `probe --phase active`, an approval decision), then go back to 1.
   `status: "no_change"` means nothing ran since the pass began.

## 5. Approvals

The crawler never performs a destructive action (delete, credential change, payment, outbound
message, deactivate, logout — and in production every data-creating action) on its own. It holds
the action in the approval queue and keeps crawling elsewhere. Any `DELETE` request is always
held; cross-role replays of POST/PUT/PATCH are not.

1. `$A approvals --run "$RUN" write` refreshes `<run>/approvals.yaml` (existing decisions, notes
   and rules are kept). `$A approvals --run "$RUN" list` prints only the pending items.
2. Edit the file. Per item set `decision` to `approve`, `deny` or `skip` and say who decided in
   `note`; leave items you do not want to decide `pending` (they appear in the map as
   unexercised surface).

   ```yaml
   items:
   - id: ap-0004
     role: admin
     category: delete
     label: Delete
     reasons: ["label matches delete keyword 'delete'"]
     predicted_request: {method: POST, url: https://staging.example.com/projects/26/delete}
     decision: approve
     note: 'operator: approve - project 26 was created by this run'
   - id: ap-0001
     role: admin
     category: logout
     label: Logout
     decision: deny
     note: 'operator: deny'
   rules:
   - match: {category: delete, url_contains: appmap-20261024-120000}
     decision: approve
   ```

   Rules match on `category`, `role`, `url_contains` and `label_contains` (all must match); the
   first matching rule wins and never overrides an existing decision. A rule also approves
   matching items queued **later** in the run without showing them to you. A rule on the run
   marker only matches when the marker appears in the URL or label (for example slug URLs);
   records addressed by numeric id need per-item decisions — check whether the id was returned
   by one of the run's own writes (the "Records created by this run" section of `APP-MAP.md`
   after a build, or the `exchanges` table).
3. `$A approvals --run "$RUN" apply` loads the decisions (`counts.changed`); problems with the
   file are reported in `errors` (`approvals.yaml: rule #2 skipped: …`).
4. `$A crawl --run "$RUN" --approved-only` replays each approved action and lets only that
   action's own request(s) through (`counts.executed`, `failed`, `skipped`). Then run a normal
   `crawl` to explore what the approved actions revealed.

Logout actions are never executed, even when approved. To revoke an approval that has not run
yet, set it to `deny` (not back to `pending`, which a matching rule could approve again).
Executed actions cannot be undone.

## 6. Output layout

Each run is one self-contained directory, `appmap-<host>-<YYYYMMDD-HHMMSS>/`:

| Path | Contents |
|---|---|
| `run.json` | Redacted config, authorization confirmation, profile, roles, status, `contains_production_data` |
| `appmap.sqlite` | The normalized map: states, transitions, routes, endpoints, params, exchanges (full bodies), locators, forms, fields, flows, value links, role access, sources, approvals, observations |
| `appmap.json` | The same public tables as one JSON document |
| `APP-MAP.md` | Human-readable map: routes, endpoints, role access matrix, flows, forms, GraphQL operations, unexercised surface, coverage gaps, records created by the run |
| `SCHEMA.md` | Table reference and query recipes (the directory documents itself) |
| `events.jsonl` | Append-only log of every action and observation, linked by `caused_by` |
| `traffic/<role>.har` | Raw HTTP per role (secrets redacted) |
| `approvals.yaml` | The approval queue and the human's decisions |
| `names.yaml` | Flow names/descriptions and form purposes, applied on every build |
| `artifacts/`, `bodies/` | Screenshot and DOM snapshot per state; large or binary bodies |
| `crawl.log` | Human-oriented log |

After the first `build`, fill in `names.yaml` (flow names, descriptions, form purposes) and run
`$A build --run "$RUN"` again.

## 7. Querying the map

```bash
$A query --run "$RUN" role-matrix --format table
$A query --run "$RUN" flows --role admin
$A query --run "$RUN" endpoint-shape --method GET --path '/api/items/{id}'
$A query --run "$RUN" forms-posting-to --endpoint '/api/%'
$A query --run "$RUN" hidden-but-reachable --role user
$A query --run "$RUN" path-to-state --state st-2f5aefe4610d
$A query --run "$RUN" flow-form-fields --flow 3
$A query --run "$RUN" gaps
$A query --run "$RUN" sql "SELECT method, path_pattern, kind FROM endpoints ORDER BY path_pattern"
```

Output is one JSON object per row (`--format table` for aligned text), followed by the status
line (`counts.rows`). `sql` accepts a single read-only `SELECT`/`WITH` statement. `SCHEMA.md`
in the run directory and `skills/query-app-map/` describe every table and more recipes;
evidence for any row is in `events.jsonl` and the HAR files.

## 8. Safety notes

- **Authorization first.** `authorization.confirmed_by` is required and recorded in
  `run.json`. Do not widen scope, switch to `production` or enable forced browsing without the
  person who authorized the run.
- **Scope and throttling** apply to browser and direct requests alike: only `allow_hosts` (and
  `allow_path_prefixes`, minus `deny_patterns`) are ever requested; a circuit breaker pauses the
  run on repeated 429/5xx. The `production` profile caps the rate at 1 request/second with
  jitter, forces concurrency 1, holds every data-creating action for approval and refuses forced
  browsing.
- **CAPTCHAs and WAF blocks** are never worked around: log that role in manually
  (`$A auth --run "$RUN" --role <role> --manual-login`), or exclude the area.
- **Credentials and session tokens** never reach the run directory: passwords, cookies, bearer
  tokens, API keys and CSRF tokens are redacted from the database, HAR, events, logs and
  artifacts. Saved sessions live outside it, in the cache directory.
- **Production data handling notice:** apart from credentials, everything is kept — full
  request and response bodies, screenshots, DOM snapshots — including on production runs. A run
  directory can contain real customer data and PII. Treat it as sensitive engagement data: keep
  it out of source control (`appmap-*/` is ignored in this repository), store it encrypted,
  share it only with people authorized for the engagement, and delete it, together with
  `~/.cache/absolute-appmap/sessions/<run_id>/`, when the engagement ends.
- **Cleanup:** generated free-text values carry the marker `appmap-<run_id>`; `APP-MAP.md`
  lists every record the run created.
- **Chromium only** (see Install).
