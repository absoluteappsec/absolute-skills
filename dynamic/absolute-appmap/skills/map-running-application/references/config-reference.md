# Config reference: `appmap.config.yaml`

One YAML file per run. `appmap init --config <file>` validates it and records its absolute path
in `run.json`; every later command re-reads it from there, so **keep the file in place**. Edits
take effect on the next command (that is how fixtures are added mid-run) — but do not change
scope, profile or roles mid-run without the operator.

Unknown keys are errors (typos are caught, not ignored). Relative paths (`fixtures`,
`storage_state_path`, `wordlists`) resolve against the config file's directory.

## Full shape

```yaml
target:
  base_urls: ["https://staging.example.com"]          # required; http(s); first one names the run dir
  scope:
    allow_hosts: ["staging.example.com", "api.staging.example.com"]  # default: hosts of base_urls
    allow_path_prefixes: ["/"]                         # default [] = whole host
    deny_patterns: ["/admin/billing/*", "*/logout"]    # globs on the URL path, case-insensitive
    login_allow_hosts: []                              # external IdP/SSO hosts: login pages only
    static_allow_hosts: []                             # CDN hosts: static subresources only
authorization:                                         # required
  confirmed_by: "Jane Doe (Security Lead)"             # required, non-empty: who confirmed
  notes: "Engagement #1234, staging only, 2026-10-24"  # optional
profile: staging                                       # staging (default) | production
limits:                                                # all optional; defaults below
  max_requests_per_second: 5
  jitter_seconds: 0
  concurrency: 4
  max_states_per_route_pattern: 5
  max_runtime_minutes: 240
  max_depth: 15
  quiet_window_ms: 500
  max_form_retries: 3
  breaker_window: 20
  breaker_threshold: 0.5
roles:
  - name: admin
    login: { type: form, url: https://staging.example.com/login, username_env: APPMAP_ADMIN_USER, password_env: APPMAP_ADMIN_PASS }
  - name: user
    login: { type: form, url: https://staging.example.com/login, username_env: APPMAP_USER_USER, password_env: APPMAP_USER_PASS }
  - name: anonymous
    login: { type: none }
  - name: sso-user
    login: { type: manual, url: https://staging.example.com/login }
  - name: api-user
    login: { type: storage_state, storage_state_path: ./api-user.state.json }
discovery:
  passive_sources: true      # robots.txt, sitemaps, /.well-known/*, manifest, source maps
  schemas: true              # OpenAPI/Swagger at common paths, GraphQL introspection
  bundle_mining: true        # API paths, routes, GraphQL documents in JS bundles
  api_replay: true           # replay captured calls with variations; cross-role replay always runs
  forced_browsing: false     # small built-in wordlist; config error with profile production
  wordlists: []              # extra wordlist files (one path per line, # comments)
form_data:
  marker_prefix: "appmap-{run_id}"   # {run_id} = YYYYMMDD-HHMMSS of the run
  fixtures: ./fixtures.yaml          # optional; see "fixtures.yaml"
spa: auto                    # auto | on | off
redact_patterns: []          # extra regexes whose matches are redacted everywhere
```

`base_urls` and `scope` may also be given at the top level instead of under `target` (not both).

## Keys

| Key | Default | Notes |
|---|---|---|
| `target.base_urls` | — | Crawl entry points. The first URL's host names the run directory |
| `scope.allow_hosts` | hosts of `base_urls` | Only these hosts are ever requested (browser and direct). Add API hosts here |
| `scope.allow_path_prefixes` | `[]` (whole host) | If set, only paths under one of these prefixes |
| `scope.deny_patterns` | `[]` | `fnmatch` globs on the path (not the host), lowercased, e.g. `*/logout`, `/admin/billing/*`. Denied URLs are never requested (on any host) |
| `scope.login_allow_hosts` | `[]` | External identity-provider hosts (SSO: `login.microsoftonline.com`, `example.okta.com`, `accounts.example-idp.com`). Admitted **only** for pages of a login flow (`appmap auth`, scripted login, re-login); never crawled, probed, replayed or recorded as endpoints (their traffic stays in the redacted HAR only) |
| `scope.static_allow_hosts` | `[]` | CDN hosts serving the app's scripts, styles, fonts and images (`cdn.example.com`, `d123.cloudfront.net`). Admitted **only** for static subresources the browser loads (and for `probe` bundle mining to read scripts and source maps); never navigated to, crawled or replayed. Add `deny_patterns` if parts of the CDN must stay untouched. A host that serves the app's API belongs in `allow_hosts` instead |
| `authorization.confirmed_by` | — | Required. Write the operator's own name/role as they stated it — never invent it |
| `authorization.notes` | `""` | Engagement id, environment, date, limits the operator mentioned |
| `profile` | `staging` | See profiles below |
| `limits.max_requests_per_second` | 5 (production: ceiling 1) | Throttle for every request |
| `limits.jitter_seconds` | 0 (production: at least 0.5) | Random extra delay per request |
| `limits.concurrency` | 4 (production: forced 1) | Validated and recorded; the v1 crawler works through roles one at a time |
| `limits.max_states_per_route_pattern` | 5 | States sampled per route pattern (lists, infinite scroll, SPA near-duplicates) |
| `limits.max_runtime_minutes` | 240 | Total browser-crawl time for the run → `limit_reached` |
| `limits.max_depth` | 15 | Maximum link/action depth from an entry point |
| `limits.quiet_window_ms` | 500 | Network-idle + no-DOM-mutation window that marks an action "settled". Raise for slow apps |
| `limits.max_form_retries` | 3 | Validation-driven resubmissions per form |
| `limits.breaker_window` | 20 | Responses considered by the circuit breaker |
| `limits.breaker_threshold` | 0.5 (production 0.2) | Fraction of 429/5xx in the window that trips it (`paused_circuit`) |
| `roles[].name` | — | Unique role name used in every table and command (`--role`) |
| `roles[].login` | `{type: none}` | See login types |
| `discovery.*` | as shown | Booleans; `wordlists` is a list of file paths |
| `form_data.marker_prefix` | `appmap-{run_id}` | Put into generated free-text values so created records can be found and cleaned up |
| `form_data.fixtures` | none | Operator/agent-supplied field values |
| `spa` | `auto` | `auto` detects a client router; `on` forces the SPA layer; `off` disables it (`true`/`false` also accepted) |
| `redact_patterns` | `[]` | Regexes for app-specific secrets (e.g. `X-Internal-Key: \w+`) |

## Profiles

| | `staging` (default) | `production` |
|---|---|---|
| Rate | `max_requests_per_second` default 5 | at most 1 (a lower value is kept), jitter at least 0.5 s |
| Concurrency | configurable | forced to 1 |
| Destructive actions | held for approval | held for approval |
| Data-creating actions (any state-changing UI submit or request) | submitted | **also held for approval** |
| Forced browsing | allowed if enabled | `forced_browsing: true` is a config error |
| Circuit breaker | trips at 50% 429/5xx | trips at 20% |
| Run directory | `contains_production_data: false` | `contains_production_data: true` + handling notice in `APP-MAP.md` |

Production values in the file can only make the run gentler, never harsher.

## Login types

| `type` | Keys | Behaviour |
|---|---|---|
| `form` | `url` (login page: absolute, or a path such as `/login` resolved against the first base URL; default: the first base URL), `username_env`, `password_env` (both required); optional `username_selector`, `password_selector`, `submit_selector`, `success_selector` | Scripted login with credentials read from the named environment variables at run time. Default selectors: username `input[type=email], input[name*=user i], input[name*=email i]`; password `input[type=password]`; submit `button[type=submit], input[type=submit]`. Success = left the login URL and no password field visible, or `success_selector` visible |
| `manual` | `url` (absolute or a path; default: the first base URL) | `appmap auth` opens a headed browser at `url`; the operator logs in (SSO, MFA) and presses Enter in the terminal. Only `appmap auth` ever asks for a manual login: when the saved session expires during `crawl`/`probe`, the role stops (`auth_failed`) instead of prompting |
| `none` | — | Anonymous |
| `storage_state` | `storage_state_path` | A Playwright storage-state JSON the operator captured |

A relative `url` (`/login`) is resolved against the first `base_urls` entry when the config is
loaded. When the login page sends the browser to an external identity provider, list that
provider's host(s) in `scope.login_allow_hosts` (and the CDN hosts its pages load assets from, if
any, too); otherwise the IdP page is blocked and the login fails.

`username`/`password` keys in the config are rejected: credentials come only from the
environment. Session state (cookies, localStorage, sessionStorage) is saved to
`~/.cache/absolute-appmap/sessions/<run_id>/<role>.json` (mode 0600), never the run directory.
`appmap auth --manual-login` turns form roles into manual ones for one invocation (use it when a
form login hits MFA or a CAPTCHA).

## fixtures.yaml

Domain values for fields the generator cannot satisfy:

```yaml
fields:
  "^(vat|tax)_?id$": "GB123456789"
  "postcode|zip": "SW1A 1AA"
  "project.?name": "{marker} demo"     # {marker} = the run's marker
  "^country$": "GB"
  "accept_terms": true
```

Keys are regexes (case-insensitive) matched against the field's name or label; the first match
wins (file order). String values may contain `{marker}`. Values override generated data.

## Examples

Staging, two roles:

```yaml
target:
  base_urls: ["https://staging.example.com"]
  scope:
    deny_patterns: ["*/logout", "*/signout"]
authorization:
  confirmed_by: "Jane Doe (Security Lead)"
  notes: "Engagement 1234; staging only; confirmed in chat 2026-10-24"
roles:
  - name: admin
    login: { type: form, url: https://staging.example.com/login, username_env: APPMAP_ADMIN_USER, password_env: APPMAP_ADMIN_PASS }
  - name: anonymous
    login: { type: none }
```

Production, throttled, one SSO role:

```yaml
target:
  base_urls: ["https://www.example.com"]
  scope:
    allow_hosts: ["www.example.com", "api.example.com"]
    deny_patterns: ["*/logout", "/account/delete*", "/billing/*"]
authorization:
  confirmed_by: "Jane Doe (Security Lead)"
  notes: "Production mapping approved by CISO, change ticket CHG-881; 1 rps max"
profile: production
limits: { max_runtime_minutes: 120 }
roles:
  - name: customer
    login: { type: manual, url: https://www.example.com/login }
```
