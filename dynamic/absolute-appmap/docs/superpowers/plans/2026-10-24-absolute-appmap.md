# absolute-appmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `absolute-appmap` plugin: a Python + Playwright engine exposed as the `appmap` CLI, plus agent-agnostic skills that drive it to crawl a running web app (SSR or SPA) as multiple roles and emit a queryable application map.

**Architecture:** One Python package (`lib/appmap/`) holds all mechanical work — config, run directory, event log, redaction, SQLite store, request gating, browser crawl with SPA layer, passive/active probing, approvals, map building, and queries — behind subcommands that each end with a machine-readable status line. Markdown skills (`skills/*/SKILL.md`) tell any agent which subcommand to run and where to apply judgment. Local Flask fixture apps (one SSR, one Vue SPA) are the test targets; acceptance is asserted against the produced `appmap.sqlite`.

**Tech Stack:** Python 3.11+, Playwright for Python (async API, Chromium), PyYAML, Jinja2, SQLite (stdlib `sqlite3`); dev: pytest, pytest-asyncio, Flask.

**Spec:** `dynamic/absolute-appmap/docs/superpowers/specs/2026-10-24-absolute-appmap-design.md` — read it alongside this plan.

## Global Constraints

- Plugin root: `absolute-skills/dynamic/absolute-appmap/`. All paths below are relative to it unless they start with `absolute-skills/`.
- Plugin name `absolute-appmap`, version `0.1.0`, license MIT, author `Absolute AppSec` (`https://absoluteappsec.com`).
- Python `>=3.11`; runtime deps only `playwright>=1.47,<2`, `pyyaml>=6`, `jinja2>=3.1`. Dev extras: `pytest>=8`, `pytest-asyncio>=0.23`, `flask>=3`.
- No MCP server dependency anywhere. Skills reference only generic actions ("run", "read") and the `scripts/appmap` wrapper.
- SKILL.md: frontmatter `name` == directory name; `description` ≤ 1024 chars, begins "Use when" or "Use during"; body = Purpose → `## Step N — …` → `## Completion Criteria` → `## Reference Files`.
- Every CLI subcommand's **last stdout line** is `APPMAP_STATUS ` followed by one JSON object with keys `command`, `ok`, `status`, `counts`, `pending_approvals`, `errors`, `next`.
- `SCHEMA_VERSION = "1.0.0"` (semver), stored in `meta`.
- Credentials and session tokens are never written in clear to any run-directory file. Everything else (customer data, full bodies, screenshots) is kept, including in production (spec decision 11).
- Cross-role POST/PUT/PATCH replays are not held for approval; destructive actions (incl. any `DELETE`) always are (spec decision 10).
- Production profile: default 1 rps, jitter 0.5 s, concurrency forced to 1, data-creating UI/replay/schema actions need approval, `forced_browsing: true` is a config error.
- Created free-text data carries `marker_prefix` (default `appmap-{run_id}`).
- Run dir name: `appmap-<target-host>-<YYYYMMDD-HHMMSS>/`. Saved sessions live in `${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/sessions/<run_id>/<role>.json`, never in the run dir.
- Venv: `${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv`.
- Commits end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Agent shell timeouts kill long crawls.** Many harnesses cap a command at ~2–10 min. Expected: `crawl`/`probe` honor `--budget-minutes` (default 8), checkpoint, exit `status: "in_progress"`, and `--resume` continues with no duplicate states. Test in Task 12.
2. **Session dies mid-crawl** (server expiry, or a logout-like control slipping past heuristics). Expected: session loss detected, role re-authenticates, step retried once, admin-only pages still reached. Test in Task 12.
3. **Secrets leak into some output file** (HAR, DOM snapshot with a filled password field, events, `appmap.json`). Expected: fixture passwords and session cookie values appear nowhere in the run dir. Test in Task 22.
4. **Scope escape via redirect or absolute URL** (in-scope page 302s to another host; bundle contains `https://third-party/...`). Expected: recorded in `external_links`, never requested. Test in Task 7.
5. **Interrupted run leaves a truncated last line in `events.jsonl`.** Expected: resume tolerates and drops the partial line rather than crashing. Test in Task 3.

---

## File Structure

```
absolute-skills/
├── .claude-plugin/marketplace.json            # Task 21
└── dynamic/absolute-appmap/
    ├── .claude-plugin/plugin.json             # Task 21
    ├── AGENTS.md  README.md  LICENSE          # Task 21
    ├── pyproject.toml                         # Task 1
    ├── commands/appmap.md  appmap-resume.md   # Task 21
    ├── scripts/setup.sh  scripts/appmap  scripts/validate.py   # Tasks 1, 21
    ├── docs/usage.md                          # Task 22
    ├── lib/appmap/
    │   ├── __init__.py          # __version__
    │   ├── cli.py               # argparse subcommands + emit_status
    │   ├── paths.py             # cache_dir(), venv/session paths
    │   ├── config.py            # RunConfig + profiles
    │   ├── rundir.py            # RunDir create/open, run.json
    │   ├── events.py            # EventLog (events.jsonl)
    │   ├── redact.py            # Redactor
    │   ├── store.py  schema.sql # SQLite DAO
    │   ├── scope.py  throttle.py  normalize.py  gate.py
    │   ├── destructive.py  approvals.py
    │   ├── recording.py         # record_exchange (shared by browser + direct)
    │   ├── direct.py            # DirectClient over Playwright APIRequestContext
    │   ├── datagen.py
    │   ├── frontier.py  crawl.py
    │   ├── browser/session.py recorder.py settle.py fingerprint.py locators.py clickables.py forms.py
    │   ├── spa/detect.py  spa/routes.py
    │   ├── probe/passive.py schemas.py bundles.py replay.py
    │   ├── gaps.py  build.py  query.py
    │   ├── templates/app-map.md.j2
    │   ├── resources/SCHEMA.md  resources/samples/{sample.txt,sample.png,sample.pdf,sample.csv}
    ├── tests/
    │   ├── conftest.py          # fixture servers, tmp run dirs, browser
    │   ├── fixtures/apps/ssr/app.py
    │   ├── fixtures/apps/spa/{app.py,static/index.html,static/app.js,static/vendor/}
    │   └── test_*.py            # one per module
    └── skills/  (8 skills, Task 21)
```

---

## Milestone A — Core engine (no browser)

### Task 1: Repository and package scaffold

**Files:**
- Create: `absolute-skills/.gitignore`, `pyproject.toml`, `lib/appmap/__init__.py`, `lib/appmap/cli.py`, `lib/appmap/paths.py`, `scripts/setup.sh`, `scripts/appmap`, `tests/test_cli.py`

**Interfaces:**
- Produces: `appmap.__version__ = "0.1.0"`; `cli.main(argv: list[str] | None = None) -> int`; `cli.emit_status(command: str, *, ok: bool, status: str, counts: dict | None = None, pending_approvals: int = 0, errors: list[str] | None = None, next: str | None = None) -> None`; `paths.cache_dir() -> Path`, `paths.venv_dir(version: str) -> Path`, `paths.session_dir(run_id: str) -> Path`. Subcommands registered (stubs until their tasks): `init, auth, crawl, probe, approvals, build, status, query`.

- [ ] **Step 1:** `git init` in `absolute-skills/`; `.gitignore` covers `.venv/`, `__pycache__/`, `appmap-*/`, `*.secrets.yaml`, `.pytest_cache/`.
- [ ] **Step 2: Write failing tests**

```python
def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out

def test_emit_status_is_last_line_json(capsys):
    emit_status("status", ok=True, status="done", counts={"states": 3})
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("APPMAP_STATUS ")
    obj = json.loads(line.removeprefix("APPMAP_STATUS "))
    assert set(obj) == {"command","ok","status","counts","pending_approvals","errors","next"}

def test_cache_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cache_dir() == tmp_path / "absolute-appmap"
```
- [ ] **Step 3:** Run `pytest tests/test_cli.py -v` → FAIL (module missing).
- [ ] **Step 4:** Implement. `pyproject.toml`: setuptools, `packages.find where=["lib"]`, package-data for `schema.sql`, `templates/*`, `resources/**`; console script `appmap = appmap.cli:main`; extras `dev`. `scripts/setup.sh`: creates venv at `paths.venv_dir`, `pip install "<plugin_dir>"`, `python -m playwright install chromium`; idempotent; `--dev` flag does `pip install -e '.[dev]'` into `./.venv` instead. `scripts/appmap`: bash, reads version from `lib/appmap/__init__.py`, runs `setup.sh` if venv missing, then `exec "$VENV/bin/appmap" "$@"`.
- [ ] **Step 5:** `pytest tests/test_cli.py -v` → PASS; `bash scripts/setup.sh --dev` completes.
- [ ] **Step 6:** Commit `feat(appmap): scaffold package, CLI skeleton, setup scripts`.

### Task 2: Run configuration and profiles

**Files:** Create `lib/appmap/config.py`, `tests/test_config.py`, `tests/data/config_staging.yaml`, `tests/data/config_prod.yaml`

**Interfaces:**
- Produces dataclasses: `Scope(allow_hosts: list[str], allow_path_prefixes: list[str], deny_patterns: list[str])`; `LoginSpec(type: Literal["form","manual","none","storage_state"], url: str|None, username_env: str|None, password_env: str|None, storage_state_path: str|None, username_selector: str|None, password_selector: str|None, submit_selector: str|None, success_selector: str|None)`; `Role(name: str, login: LoginSpec)`; `Authorization(confirmed_by: str, notes: str)`; `Limits(max_requests_per_second: float, jitter_seconds: float, concurrency: int, max_states_per_route_pattern: int, max_runtime_minutes: int, max_depth: int, quiet_window_ms: int, max_form_retries: int, breaker_window: int, breaker_threshold: float)`; `Discovery(passive_sources: bool, schemas: bool, bundle_mining: bool, api_replay: bool, forced_browsing: bool, wordlists: list[str])`; `FormData(marker_prefix: str, fixtures: str|None)`; `RunConfig(base_urls: list[str], scope: Scope, authorization: Authorization, profile: Literal["staging","production"], limits: Limits, roles: list[Role], discovery: Discovery, form_data: FormData, spa: Literal["auto","on","off"], redact_patterns: list[str], source_path: Path)`.
- `load_config(path: Path) -> RunConfig`; `class ConfigError(ValueError)`; `RunConfig.credentials(role: str) -> tuple[str, str] | None` (reads env vars at call time only); `RunConfig.to_redacted_dict() -> dict`.

Defaults (staging / production): rps 5 / 1; jitter 0 / 0.5; concurrency 4 / forced 1; breaker_threshold 0.5 / 0.2; breaker_window 20; max_states_per_route_pattern 5; max_runtime_minutes 240; max_depth 15; quiet_window_ms 500; max_form_retries 3. Missing `scope.allow_hosts` defaults to hosts of `base_urls`.

- [ ] **Step 1: Write failing tests**

```python
def test_staging_defaults(): cfg = load_config(DATA/"config_staging.yaml"); assert cfg.limits.max_requests_per_second == 5 and cfg.limits.concurrency == 4
def test_production_forces_concurrency_and_rate():
    cfg = load_config(DATA/"config_prod.yaml")  # file sets concurrency: 8
    assert cfg.limits.concurrency == 1 and cfg.limits.max_requests_per_second == 1 and cfg.limits.jitter_seconds == 0.5
def test_production_forced_browsing_rejected(tmp_path): ...  # forced_browsing: true + profile production -> ConfigError
def test_missing_authorization_rejected(tmp_path): ...       # no authorization.confirmed_by -> ConfigError
def test_inline_password_rejected(tmp_path): ...             # login.password key present -> ConfigError("credentials must come from env")
def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("APPMAP_ADMIN_USER","admin"); monkeypatch.setenv("APPMAP_ADMIN_PASS","pw")
    assert load_config(DATA/"config_staging.yaml").credentials("admin") == ("admin","pw")
def test_redacted_dict_has_no_env_values(monkeypatch): ...   # to_redacted_dict() contains env var NAMES, never values
```
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(appmap): run config with staging/production profiles`.

### Task 3: Run directory, event log, redaction

**Files:** Create `lib/appmap/rundir.py`, `events.py`, `redact.py`, `tests/test_rundir.py`, `tests/test_events.py`, `tests/test_redact.py`

**Interfaces:**
- `RunDir` (dataclass): `root, run_id, events_path, traffic_dir, artifacts_dir, bodies_dir, db_path, approvals_path, json_path, map_md_path, schema_md_path, log_path`. `create_run_dir(cfg: RunConfig, parent: Path, now: datetime | None = None) -> RunDir`; `open_run_dir(path: Path) -> RunDir`; `write_run_meta(rd: RunDir, cfg: RunConfig, *, status: str, extra: dict | None = None) -> None` (writes `run.json` with redacted config, authorization, `tool_version`, `schema_version`, `profile`, `roles`, `contains_production_data: profile == "production"`, timings, status).
- `Event(id: str, ts: float, type: str, role: str|None, caused_by: str|None, data: dict)`; event `type` ∈ `navigate, click, fill, submit, request, response, dialog, state_entered, approval_queued, approval_executed, login, session_lost, websocket, sse, error, observation`. `EventLog(path: Path, redactor: Redactor)`: `.emit(type: str, *, role=None, caused_by=None, **data) -> Event` (ids `ev-000001` monotonic, continuing after reopen); `.iter() -> Iterator[Event]`.
- `Redactor(secret_values: list[str], extra_patterns: list[str])`: `.text(s) -> str`, `.headers(h: dict[str,str]) -> dict`, `.url(u) -> str`, `.json(obj) -> obj`, `.form(pairs: dict) -> dict`. Placeholders: `<redacted:authorization>`, `<redacted:cookie>`, `<redacted:jwt>`, `<redacted:password>`, `<redacted:csrf>`, `<redacted:api_key>`, `<redacted:secret>`. Cookie redaction keeps cookie names. Field names matching `pass|pwd|secret|token|api[-_]?key|csrf|xsrf` are redacted by key.

- [ ] **Step 1: Write failing tests**

```python
def test_run_dir_name(tmp_path, cfg): rd = create_run_dir(cfg, tmp_path, datetime(2026,10,24,9,5,1)); assert rd.root.name == "appmap-staging.example.com-20261024-090501"
def test_run_json_flags_production(tmp_path, prod_cfg): ... assert json.load(open(rd.root/"run.json"))["contains_production_data"] is True
def test_event_ids_continue_after_reopen(tmp_path): ...  # emit 2, reopen, emit -> "ev-000003"
def test_truncated_last_line_tolerated(tmp_path):        # Review Focus 5
    p = tmp_path/"events.jsonl"; p.write_text('{"id":"ev-000001","ts":1,"type":"navigate","role":null,"caused_by":null,"data":{}}\n{"id":"ev-0000')
    log = EventLog(p, Redactor([], [])); assert [e.id for e in log.iter()] == ["ev-000001"]; assert log.emit("click").id == "ev-000002"
def test_redacts_jwt_cookie_auth_and_known_secret():
    r = Redactor(["hunter2"], [])
    assert r.headers({"Authorization":"Bearer eyJa.eyJb.c"})["Authorization"] == "<redacted:authorization>"
    assert r.headers({"Cookie":"sid=abc; theme=dark"})["Cookie"] == "sid=<redacted:cookie>; theme=<redacted:cookie>"
    assert "hunter2" not in r.text("pw=hunter2") and r.json({"password":"x","name":"n"}) == {"password":"<redacted:password>","name":"n"}
def test_customer_data_not_redacted(): assert Redactor([],[]).json({"email":"a@b.com"}) == {"email":"a@b.com"}  # decision 11
```
- [ ] **Step 2:** FAIL → **Step 3:** implement (on open, `EventLog` truncates a trailing partial line in place) → **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(appmap): run directory, event log, redaction`.

### Task 4: SQLite store

**Files:** Create `lib/appmap/schema.sql`, `lib/appmap/store.py`, `tests/test_store.py`

**Interfaces:**
- `SCHEMA_VERSION = "1.0.0"` in `store.py`. `Store(db_path: Path)`, `.init_schema()`, `.conn` (sqlite3, WAL, row_factory=Row), `Store.open_readonly(db_path) -> Store` (URI `mode=ro`).
- Tables (columns; all have integer `id` PK unless noted; `*_event` columns hold `events.jsonl` ids):
  - `meta(key PK, value)`; `roles(name PK, login_type, auth_ok, last_login_event)`
  - `states(id TEXT PK, role, url, route_pattern, dom_fingerprint, title, first_seen_event, screenshot, dom_snapshot, visits)`
  - `transitions(from_state, to_state, role, action_type, action_label, locator_set_id, event_id, exchange_ids JSON)`
  - `locator_sets(id, primary_strategy)`; `locators(locator_set_id, strategy, value, rank)` — strategies `testid, role_name, label, text, css, xpath`
  - `routes(pattern, host, kind CHECK in ('page','spa_route'), UNIQUE(host,pattern,kind))`
  - `endpoints(method, host, path_pattern, kind CHECK in ('rest','graphql_op','websocket','sse','static','page'), graphql_op, graphql_type, content_types JSON, UNIQUE(method,host,path_pattern,graphql_op))`
  - `params(endpoint_id, location CHECK in ('path','query','body','header','cookie'), name, types JSON, examples JSON, required_evidence, UNIQUE(endpoint_id,location,name))`
  - `exchanges(endpoint_id, role, origin CHECK in ('ui','replay','cross_role','schema','passive','bundle','approved','login'), event_id, method, url, req_headers JSON, req_body, req_body_path, status, resp_headers JSON, resp_body, resp_body_path, content_type, duration_ms, ts)`
  - `responses(endpoint_id, role, statuses JSON, content_types JSON, body_shape JSON, min_size, max_size, sample_exchange_ids JSON, UNIQUE(endpoint_id, role))`
  - `forms(state_id, inferred BOOL, method, action_endpoint_id, purpose, submit_locator_set_id, succeeded BOOL)`; `fields(form_id, name, type, widget, constraints JSON, options JSON, locator_set_id, values_tried JSON, validation_messages JSON)`
  - `flows(name, role, description, preconditions JSON)`; `flow_steps(flow_id, ordinal, transition_id, exchange_id)`; `value_links(producer_exchange_id, producer_json_path, consumer_exchange_id, consumer_location, value_hash)`
  - `role_access(item_kind CHECK in ('route','endpoint'), item_id, role, outcome CHECK in ('allowed','redirect_login','forbidden','not_found','different_content','error','not_tested'), evidence_exchange_id, UNIQUE(item_kind,item_id,role))`
  - `sources(item_kind, item_id, source CHECK in ('ui','spa_router','bundle','openapi','graphql','sitemap','robots','well_known','source_map','replay','wordlist'), UNIQUE(item_kind,item_id,source))`
  - `approvals(id TEXT PK, role, state_id, category, label, reasons JSON, action_path JSON, predicted_request JSON, decision CHECK in ('pending','approve','deny','skip'), decided_by, executed_event)`
  - `observations(kind, detail, url, role, event_id)`; `external_links(url, found_on, role, UNIQUE(url,found_on))`
  - `frontier(id, role, kind CHECK in ('url','action'), url, action_path JSON, depth, priority, status CHECK in ('pending','done','skipped','queued_approval','failed'), source, UNIQUE(role,kind,url,action_path))`
- DAO methods, one per table, named `upsert_<singular>` (idempotent on the UNIQUE key, returns id) or `add_<singular>` (append, returns id), plus `get_meta/set_meta`. Later tasks call these by those names.

- [ ] **Step 1: Write failing tests** — `test_schema_version_recorded` (`get_meta("schema_version") == "1.0.0"`); `test_upsert_endpoint_idempotent` (same key twice → same id); `test_role_access_rejects_bad_outcome` (`sqlite3.IntegrityError`); `test_readonly_store_rejects_writes`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): sqlite schema v1.0.0 and store`.

### Task 5: Scope, throttle, circuit breaker, path normalization, gate

**Files:** Create `scope.py`, `throttle.py`, `normalize.py`, `gate.py`, `tests/test_scope.py`, `tests/test_throttle.py`, `tests/test_normalize.py`

**Interfaces:**
- `ScopeGuard(scope: Scope)`: `.allows(url: str) -> bool` (scheme http/https, host in allow_hosts, path under a prefix, not matching any `fnmatch` deny pattern against the path).
- `RateLimiter(rps: float, jitter: float)`: `async .acquire() -> None`. `CircuitBreaker(window: int, threshold: float)`: `.record(status: int) -> None`, `.tripped -> bool` (fraction of 429/5xx in last `window` ≥ threshold, only once window full). `class CircuitOpen(RuntimeError)`.
- `normalize_path(path: str, known_patterns: list[str] = []) -> str`: first match against known router/schema patterns (`:id`, `[id]`, `{id}` all normalized to `{id}`), else replace segments: integer → `{id}`, UUID → `{uuid}`, hex ≥16 → `{hash}`, segment containing the run marker → `{slug}`. Query string dropped.
- `RequestGate(scope: ScopeGuard, limiter: RateLimiter, breaker: CircuitBreaker, store: Store)`: `async .admit(method: str, url: str, *, role: str, found_on: str | None) -> bool` (False + `store.upsert_external_link` when out of scope; raises `CircuitOpen` when tripped; awaits limiter otherwise); `.observe(status: int) -> None`.

- [ ] **Step 1: Tests** — `test_deny_pattern_blocks_logout` (`*/logout` denies `/app/logout`); `test_other_host_out_of_scope`; `test_rate_limiter_spacing` (5 acquires at rps=10 take ≥0.4s); `test_breaker_trips_at_threshold` (window 10, threshold 0.2, two 503s + eight 200s → tripped); `test_normalize` table: `/users/42 → /users/{id}`, `/o/3f2a…uuid → /o/{uuid}`, `/p/appmap-r1-x → /p/{slug}`, `/items/7` with known `/items/:itemId` → `/items/{itemId}`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): scope guard, throttle, breaker, path normalization`.

### Task 6: Destructive-action classifier and approval queue

**Files:** Create `destructive.py`, `approvals.py`, `tests/test_destructive.py`, `tests/test_approvals.py`

**Interfaces:**
- `ActionDescriptor(kind: Literal["click","submit","request"], method: str|None, url: str|None, label: str, context_text: str, has_confirm: bool, role: str, origin: str)` (origin values as `exchanges.origin`).
- `Flag(held: bool, category: str|None, reasons: list[str])`; categories `delete, credential_change, payment, outbound_message, deactivate, logout, data_create`.
- `classify(a: ActionDescriptor, profile: str) -> Flag`. Rules in order: logout/sign-out label or URL → `logout`; method DELETE → `delete`; keyword match on label/URL/context (word lists in `destructive.py`: delete/remove/destroy/purge; change/reset password/email; pay/purchase/checkout/refund/transfer; send/invite/notify/email/sms; deactivate/disable/revoke/cancel subscription|account) → that category; `has_confirm` → `delete` unless another category matched; then **if `origin == "cross_role"` and method in POST/PUT/PATCH → not held** (decision 10); then production and method in POST/PUT/PATCH (or kind submit) → `data_create`. Otherwise not held.
- `PendingAction(id, role, state_id, category, label, reasons, action_path: list[dict], predicted_request: dict|None)`. `ApprovalQueue(store: Store, yaml_path: Path)`: `.enqueue(pa) -> str` (ids `ap-0001`…, dedup on role+action_path), `.write_yaml() -> None`, `.load_decisions() -> int` (applies item decisions and `rules`; returns count changed), `.approved_unexecuted() -> list[PendingAction]`, `.mark_executed(id, event_id)`, `.counts() -> dict[str,int]`.
- `approvals.yaml` shape: `items: [{id, role, category, label, reasons, predicted_request, decision, note}]`, `rules: [{match: {category?, role?, url_contains?, label_contains?}, decision}]`. Item-level decisions win over rules.

- [ ] **Step 1: Tests**

```python
@pytest.mark.parametrize("label,method,origin,profile,held,cat", [
  ("Delete project","POST","ui","staging",True,"delete"),
  ("Save","DELETE","replay","staging",True,"delete"),
  ("Sign out","GET","ui","staging",True,"logout"),
  ("Change password","POST","ui","staging",True,"credential_change"),
  ("Save","POST","ui","staging",False,None),
  ("Save","POST","ui","production",True,"data_create"),
  ("Save","PUT","cross_role","production",False,None),     # decision 10
  ("Remove","DELETE","cross_role","staging",True,"delete"),
])
def test_classify(...): ...
def test_rule_approves_marker_deletes(tmp_path): ...  # rule {category: delete, url_contains: "appmap-"} approves only the matching item
def test_item_decision_beats_rule(tmp_path): ...
def test_enqueue_dedups(tmp_path): ...
```
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): destructive-action classifier and approval queue`.

### Task 7: Exchange recording and direct client

**Files:** Create `recording.py`, `direct.py`, `tests/test_recording.py`, `tests/test_direct.py`

**Interfaces:**
- `record_exchange(store, events, redactor, rd: RunDir, *, role: str, origin: str, method: str, url: str, req_headers: dict, req_body: bytes|None, status: int, resp_headers: dict, resp_body: bytes|None, duration_ms: float, caused_by: str|None, known_patterns: list[str]) -> int` — emits `request`+`response` events, redacts headers/bodies/URL, stores bodies ≤256 KiB text inline else writes `bodies/<exchange_id>.<ext>`, detects GraphQL (JSON body with `query`; op type+name via `^\s*(query|mutation|subscription)\s+(\w+)`, falling back to `operationName`) → `kind="graphql_op"`, `text/event-stream` → `sse`, static asset extensions → `static`, HTML document → `page`; upserts endpoint, params (path/query/body JSON keys/form keys), `responses` summary (body_shape = JSON key skeleton with types, arrays collapsed to first element), `sources`, and `observations` via `detect_observations(status: int, body: str|None, content_type: str|None, headers: dict) -> list[tuple[str,str]]` (kinds: `server_error` for 5xx, `stack_trace` for Python/Java/.NET/Node/Ruby trace patterns, `debug_page` for Werkzeug/Django/Rails/Laravel debug markers, `captcha` for reCAPTCHA/hCaptcha/Turnstile markers, `rate_limited` for 429/`Retry-After`). Returns exchange id.
- `DirectClient(gate: RequestGate, rec: Callable[..., int])`: `async .request(api: APIRequestContext, method, url, *, role, origin, caused_by=None, headers=None, data=None, json=None, params=None, extra_auth: dict | None = None) -> tuple[int, APIResponse] | None` — returns None when the gate refuses; never follows redirects automatically (`max_redirects=0`), each redirect hop passes through the gate.

- [ ] **Step 1: Tests** (use a local Flask stub from `conftest.py`) — `test_graphql_keyed_by_operation` (two POSTs to `/graphql` with `query ListItems` and `mutation DeleteItem` → two endpoints with `graphql_op` set); `test_large_body_goes_to_file`; `test_redirect_to_external_host_not_followed` (Review Focus 4: stub `/go` 302 → `https://evil.example/x`; result: no request to evil host, `external_links` has it); `test_body_shape_skeleton` (`{"items":[{"id":1,"name":"a"}],"next":null}` → `{"items":[{"id":"int","name":"str"}],"next":"null"}`); `test_observations_detected` (500 with `Traceback (most recent call last)` → `server_error` + `stack_trace` rows).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): exchange recording and gated direct client`.

---

## Milestone B — Test targets and browser layer

### Task 8: Fixture applications

**Files:** Create `tests/fixtures/apps/ssr/app.py`, `tests/fixtures/apps/spa/app.py`, `tests/fixtures/apps/spa/static/{index.html,app.js}`, `tests/fixtures/apps/spa/static/vendor/{vue.global.prod.js,vue-router.global.prod.js}`, `tests/conftest.py`, `tests/test_fixtures.py`

**Interfaces:**
- Produces pytest fixtures: `ssr_url -> str`, `spa_url -> str` (Werkzeug `make_server` on port 0 in a thread, session-scoped, state reset per test via `POST /__reset`), `browser` (session-scoped async Chromium), `make_cfg(base_url, roles=..., profile="staging", **overrides) -> RunConfig`, `run_dir(tmp_path, cfg) -> RunDir`. Env creds: `APPMAP_ADMIN_USER/PASS=admin/admin-pw-Fx1`, `APPMAP_ALICE_USER/PASS=alice/alice-pw-Fx2`.
- SSR app surface (exact, assertions depend on it): `/` links to `/projects`, `/login`, `/logout`; `/login` form (`username`,`password`), cookie session `sid`, sessions expire after 200 requests; `/projects?page=N` (3 pages); `/projects/new` form (`name` required, `pattern="[a-z0-9-]{3,40}"`, `visibility` select public/private, `due` date); `/projects/<int:id>`; `/projects/<id>/delete` POST form with `onsubmit="return confirm('Are you sure?')"`; `/admin` (admin only; alice gets 403; no link for alice); `/account/password` form; `/robots.txt` (`Disallow: /secret-report`); `/sitemap.xml` (lists `/`, `/projects`, `/about`); `/about`; `/secret-report`; `/api/openapi.json` (GET `/api/projects`, GET `/api/projects/{id}`, GET `/api/internal/stats`); those API routes; `/go-external` 302 → `https://external.invalid/`.
- SPA app (Vue 3.4.38 + vue-router 4.4.3, vendored, history mode): routes `/`, `/items`, `/items/:id`, `/settings` (not linked anywhere), `/admin` (client-guarded, role from token); login posts `/api/login` → JWT-shaped token saved in `localStorage.token`, sent as `Authorization: Bearer`; `<div class="card" @click>` navigation without href; a non-`<form>` input group (`title` controlled input, custom combobox `role=combobox`) with a "Save" button → `POST /api/items`; infinite list via `GET /api/items?cursor=` (10 pages); `POST /graphql` supporting `query ListItems` and `mutation DeleteItem`; `GET /api/events` SSE; bundle `app.js` contains string `"/api/internal/flags"` (endpoint exists, never called).

- [ ] **Step 1:** Download the two vendor files from `https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js` and `https://unpkg.com/vue-router@4.4.3/dist/vue-router.global.prod.js`; commit them (MIT; add header comment noting source).
- [ ] **Step 2: Tests** — `test_ssr_login_and_admin_gate` (admin 200, alice 403 on `/admin`); `test_spa_serves_index_for_deep_link` (`/items/3` returns index.html); `test_spa_graphql_ops`.
- [ ] **Step 3–4:** implement → PASS.
- [ ] **Step 5:** Commit `test(appmap): SSR and SPA fixture applications`.

### Task 9: Role sessions, login, traffic recorder

**Files:** Create `browser/session.py`, `browser/recorder.py`, `tests/test_session.py`, `tests/test_recorder.py`

**Interfaces:**
- `RoleSession(role: Role, context: BrowserContext, auth_headers: dict[str,str], session_storage: dict[str,str])`.
- `async open_role_session(browser, cfg, rd, role: Role, recorder: TrafficRecorder) -> RoleSession` — context with `service_workers="block"`, `record_har_path=rd.traffic_dir/f"{role.name}.har"`, `record_har_content="embed"`; restores storage state from `paths.session_dir` if present.
- `async login(sess, cfg, events) -> bool` per type: `form` (selectors from LoginSpec or defaults: username `input[type=email], input[name*=user i], input[name*=email i]`, password `input[type=password]`, submit `button[type=submit], input[type=submit]`; success = `success_selector` visible, else URL no longer equals login URL and no password field visible); `none`; `storage_state` (load file); `manual` (headed browser, prints instructions, waits for operator to press Enter in terminal — `--manual-login` flag on `appmap auth`). After success: save storage state + `sessionStorage` snapshot to `paths.session_dir(run_id)/<role>.json`; `upsert_role`; emit `login`.
- `is_session_lost(sess, page, last_status: int | None, login_url: str | None) -> bool` — 401, redirect onto login URL, or visible password field on a non-login page.
- `async ensure_authenticated(sess, page, cfg, events) -> bool` — on loss: emit `session_lost`, re-login, return result.
- `TrafficRecorder(store, events, redactor, rd, gate: RequestGate, known_patterns: list[str])`: `.attach(context, role)` — installs `context.route("**/*")` that calls `gate.admit` for every browser request (out of scope → `route.abort()`, which also covers redirects and third-party subresources; throttle applies to documents, XHR/fetch, and websockets, not to static subresources of in-scope hosts); on `requestfinished` calls `record_exchange(origin="ui")`; learns `auth_headers` (Authorization, X-CSRF-Token-like) per role into the `RoleSession`; `page.on("websocket")` → endpoint `websocket` + `websocket` events per frame (redacted); `.finalize_har(role)` rewrites the HAR through `Redactor`.

- [ ] **Step 1: Tests** — `test_browser_external_redirect_aborted` (navigate `/go-external` → no request reaches `external.invalid`, `external_links` row exists); `test_form_login_ssr` (admin logs in; storage state file exists under session dir, **not** under run dir); `test_spa_bearer_token_learned` (after SPA login, `auth_headers["Authorization"]` starts with `Bearer`); `test_har_redacted` (no `admin-pw-Fx1` and no raw `sid` value in `traffic/admin.har`); `test_session_loss_detected_and_recovered` (`POST /__expire` then navigate `/admin` → `ensure_authenticated` True, page 200).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): role sessions, login types, traffic recorder`.

### Task 10: Settle, fingerprint, locators, clickables

**Files:** Create `browser/settle.py`, `browser/fingerprint.py`, `browser/locators.py`, `browser/clickables.py`, tests for each

**Interfaces:**
- `async wait_settled(page, quiet_ms: int, timeout_ms: int = 15000) -> bool` — inflight-request counter (page events) and a `MutationObserver` installed via init script; settled when both quiet for `quiet_ms`.
- `SKELETON_JS` (returns list of tokens: `tag[role]` per element up to depth 12, skipping `script/style/svg` internals, collapsing runs of identical sibling tokens into one); `dom_fingerprint(tokens: list[str]) -> str` (sha1 hex, 16 chars); `state_id(route_pattern: str, fingerprint: str, role: str) -> str` (`st-` + sha1(role|pattern|fp)[:12]).
- `LOCATOR_JS` (given element → `{testid, role, name, label, text, css, xpath}`; testid from `data-testid|data-test|data-cy`); `rank_locators(raw: dict) -> list[dict]` (order: testid, role_name, label, text, css, xpath; drop empties; `{strategy, value, rank}`); `async resolve(page, locators: list[dict]) -> Locator | None` (first strategy that matches exactly one element).
- `Clickable(locators: list[dict], tag: str, role: str|None, label: str, href: str|None, reason: str)`; `async enumerate_clickables(page) -> list[Clickable]` — anchors with href, ARIA roles `button|link|tab|menuitem|option`, `[onclick]`, computed `cursor:pointer`, plus CDP `DOMDebugger.getEventListeners` for `click`/`mousedown`/`pointerdown` on candidate elements (cap 2000); dedup by top locator. `async expand_reveals(page) -> int` — clicks `[aria-expanded=false]`, `[role=tab]` not selected, `details:not([open]) summary`, hovers nav items; returns count revealed.

- [ ] **Step 1: Tests** — `test_fingerprint_ignores_text_and_counts` (two pages differing only in text and list length → equal); `test_fingerprint_differs_on_structure`; `test_spa_div_click_found` (SPA `/` → a clickable with reason `listener` and label of the card); `test_locator_ranking_prefers_testid`; `test_resolve_falls_back_to_css`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): settledness, DOM fingerprints, locators, clickable discovery`.

### Task 11: Forms — discovery, data generation, fill and submit

**Files:** Create `browser/forms.py`, `datagen.py`, `lib/appmap/resources/samples/*`, `tests/test_datagen.py`, `tests/test_forms.py`

**Interfaces:**
- `FieldInfo(name, type, widget: Literal["input","select","multiselect","checkbox","radio","textarea","date","combobox","richtext","file"], required, pattern, min, max, maxlength, options: list[str], locators: list[dict])`; `FormInfo(inferred: bool, method: str|None, action: str|None, fields: list[FieldInfo], submit_locators: list[dict], has_confirm: bool, label: str)`.
- `async discover_forms(page) -> list[FormInfo]` — real `<form>`s, plus inferred: nearest common container of ≥1 input and a button whose text matches `save|submit|create|add|update|send|search|next|continue` and that is not inside a `<form>`.
- `DataGen(marker: str, fixtures: dict)`; `.value_for(field: FieldInfo, attempt: int, hint: str | None = None) -> str | list[str] | Path | bool`. By name/type: email `"{marker}@example.test"`, phone `"+15555550100"`, url `"https://example.test/{marker}"`, number midpoint of min/max (default 1), date today, select first non-empty option, free text `"{marker}"` trimmed/adjusted to `pattern`/`maxlength` (generate from pattern for simple char-class patterns), file → sample by `accept`. Fixtures (`fixtures.yaml`: `fields: {<regex on name or label>: value}`) override.
- `hint_from_validation(message: str) -> str | None` (maps "too short/long", "invalid email", "must match"…).
- `SubmitResult(success: bool, attempts: int, validation_messages: dict[str, list[str]], exchange_ids: list[int], to_url: str)`.
- `async fill_and_submit(page, form, gen: DataGen, *, max_retries: int, settle_ms: int) -> SubmitResult` — fills with `press_sequentially` for text, `select_option`, combobox: type then choose first `[role=option]`, richtext via `contenteditable` typing, file via `set_input_files`; success = 2xx/3xx triggered request with no inline `:invalid`/`[aria-invalid=true]`/error-text and no 4xx field errors; retries with hints.

- [ ] **Step 1: Tests** — `test_value_respects_pattern_and_marker` (pattern `[a-z0-9-]{3,40}` → matches and starts with marker); `test_fixture_override`; `test_ssr_new_project_submits` (success, a `POST /projects/new` exchange exists); `test_spa_inferred_form_with_combobox` (inferred form found, submit success, `POST /api/items` body contains marker); `test_validation_retry_recorded` (fixture rejects first submit when `name` shorter than 5 via server rule → attempts==2, messages recorded).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): form discovery, data generation, fill/submit loop`.

---

## Milestone C — Crawl, discovery, approvals

### Task 12: Frontier and crawler

**Files:** Create `frontier.py`, `crawl.py`, `tests/test_frontier.py`, `tests/test_crawl.py`; Modify `cli.py` (`init`, `auth`, `crawl`)

**Interfaces:**
- `action_path` item shapes: `{"type":"goto","url":str}`, `{"type":"click","locators":[...],"label":str}`, `{"type":"submit","form_label":str,"locators":[...]}`.
- `Frontier(store)`: `.push(role, kind, *, url=None, action_path=None, depth, priority=0, source) -> bool` (False if duplicate), `.pop(role) -> FrontierItem | None` (highest priority, then lowest depth), `.mark(item_id, status)`, `.pending_count(role=None) -> int`.
- `CrawlStats(states: int, transitions: int, forms: int, exchanges: int, queued_approvals: int, status: Literal["done","in_progress","paused_circuit","limit_reached"])`.
- `Crawler(cfg, rd, store, events, gate, queue, recorder, sessions: dict[str, RoleSession], gen: DataGen)`: `async .run(roles: list[str], *, budget_minutes: float, approved_only: bool = False) -> CrawlStats`.
- Per item: restore (goto entry URL, replay `action_path` via `resolve`; abort item as `failed` if a step can't resolve) → `wait_settled` → `ensure_authenticated` (retry once) → fingerprint → `upsert_state` (screenshot `artifacts/<state_id>.png` and redacted DOM `artifacts/<state_id>.html`, password inputs' values stripped, first visit only) → if `visits` for that `route_pattern` exceeds `max_states_per_route_pattern`, stop expanding (infinite lists/pagination) → `expand_reveals` → push anchors (out of scope → `external_links`), clickables, forms as child action items (depth+1, ≤ `max_depth`) → when executing an action: build `ActionDescriptor`, `classify`; held → `queue.enqueue` + frontier `queued_approval`; else perform, `add_transition` with `locator_set` and triggered exchange ids.
- Dialogs: `alert/prompt` recorded and accepted; `confirm` on a non-approved action → dismissed, action enqueued (category `delete` unless classifier says otherwise).
- Budget/limits: stop cleanly when `budget_minutes` or `max_runtime_minutes` elapse → `in_progress`/`limit_reached`; `CircuitOpen` → `paused_circuit`. All state is in SQLite so a later `crawl` resumes.
- Every subcommand also appends a human-oriented log to `crawl.log` in the run dir (stdlib `logging`, redacted).
- CLI: `appmap init --config C --out DIR` (creates run dir, prints its path, status `next: "appmap auth --run <dir>"`); `appmap auth --run R [--role N] [--manual-login]`; `appmap crawl --run R [--role N] [--budget-minutes 8] [--approved-only]`. `run.json` stores the config path; `--run` is the only handle later commands need.

- [ ] **Step 1: Tests**
```python
async def test_ssr_crawl_admin_and_alice(ssr_url, ...):
    stats = await crawl(cfg, roles=["admin","alice","anonymous"], budget=5)
    assert stats.status == "done"
    pages = {r["pattern"] for r in q("select pattern from routes")}
    assert {"/", "/projects", "/projects/{id}", "/projects/new", "/admin", "/account/password"} <= pages
    assert q1("select count(*) from approvals where category='delete'") >= 1        # project delete held
    assert q1("select count(*) from approvals where category='logout'") >= 1
    assert q1("select count(*) from approvals where category='credential_change'") >= 1
    assert q1("select count(*) from exchanges where url like '%/delete%'") == 0     # never executed
async def test_spa_crawl_states_and_list_sampling(spa_url, ...):
    # /items infinite list -> bounded states, div-click transitions recorded, SSE endpoint kind 'sse'
async def test_budget_checkpoint_and_resume(ssr_url, ...):                       # Review Focus 1
    s1 = await crawl(..., budget=0.02); assert s1.status == "in_progress"
    s2 = await crawl(..., budget=5);   assert s2.status == "done"
    assert q1("select count(*) from states") == q1("select count(distinct id) from states")
async def test_session_expiry_mid_crawl(ssr_url, ...):                           # Review Focus 2
    # fixture expires sessions every 200 requests; admin still reaches /admin; events include session_lost and login
```
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): frontier, state-graph crawler, init/auth/crawl commands`.

### Task 13: SPA detection and route extraction

**Files:** Create `spa/detect.py`, `spa/routes.py`, `tests/test_spa.py`; Modify `crawl.py` (seed frontier with SPA routes per role; install history-patch init script)

**Interfaces:**
- `DETECT_JS` → `{framework: "react"|"vue"|"angular"|"next"|"nuxt"|"sveltekit"|"unknown", has_router: bool}`; `async detect_spa(page) -> dict`.
- `SpaRoute(pattern: str, source: str)`; `async extract_routes(page, base_url: str, fetch_text: Callable[[str], Awaitable[str|None]]) -> list[SpaRoute]` — Vue: `__vue_app__.config.globalProperties.$router.getRoutes()`; Nuxt: `$nuxt.$router.getRoutes()` / `useRouter`; Next: `__NEXT_DATA__.buildId` → fetch `/_next/static/<buildId>/_buildManifest.js`, parse page keys; SvelteKit: `__sveltekit_*` route manifest when exposed; React Router / Angular: no runtime hook — return [] (bundle mining covers them, Task 15).
- `HISTORY_PATCH_JS` init script wraps `pushState/replaceState` and listens to `hashchange`/`popstate`, appending to `window.__appmap_nav`; crawler reads it after each action to record client route changes as transitions even when DOM fingerprint is unchanged.
- Crawler: when `cfg.spa != "off"` and detection finds a router (or `cfg.spa == "on"`), `upsert_route(kind="spa_route")`, `add_source("spa_router")`, push each concrete route (patterns with params filled by IDs observed in exchanges, else skipped and listed) as `goto` items for **every** role.

- [ ] **Step 1: Tests** — `test_detect_vue_router`; `test_extract_vue_routes` (`{"/", "/items", "/items/:id", "/settings", "/admin"}` normalized to `{id}`); `test_next_build_manifest_parsing` (pure parse of a fixture `_buildManifest.js` string); `test_unlinked_settings_route_crawled` (after SPA crawl, a state exists with `route_pattern="/settings"` and its source is `spa_router`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): SPA detection, router extraction, history tracking`.

### Task 14: Passive sources and schemas

**Files:** Create `probe/passive.py`, `probe/schemas.py`, `tests/test_probe_passive.py`, `tests/test_schemas.py`; Modify `cli.py` (`probe --phase passive`)

**Interfaces:**
- `async probe_passive(client: DirectClient, api, cfg, store, frontier, role: str) -> dict[str,int]` — `robots.txt` (Allow/Disallow/Sitemap paths → routes, source `robots`), `sitemap.xml` incl. sitemap indexes (depth ≤3, source `sitemap`), `/.well-known/security.txt`, `/.well-known/openid-configuration`, `/manifest.json`, `/.well-known/` listing attempts only for those names; each discovered in-scope URL pushed to frontier for every role.
- `parse_robots(text) -> list[str]`, `parse_sitemap(xml: str) -> tuple[list[str], list[str]]` (urls, child sitemaps).
- `async probe_schemas(client, api, cfg, store, role) -> dict[str,int]` — OpenAPI at `/openapi.json, /swagger.json, /v3/api-docs, /api-docs, /swagger/v1/swagger.json, /api/openapi.json` plus any `*.json` URL seen containing `openapi`/`swagger`; `parse_openapi(doc: dict) -> list[SchemaOp(method, path_pattern, params: list[dict])]` (OpenAPI 2 and 3). GraphQL: endpoints already of kind `graphql_op` or at `/graphql, /api/graphql` → introspection query → `parse_introspection(result) -> list[tuple[type, name]]` → endpoints `graphql_op`, source `graphql`.
- CLI: `appmap probe --run R --phase passive [--budget-minutes 8]`.

- [ ] **Step 1: Tests** — `test_parse_robots`; `test_parse_sitemap_index`; `test_parse_openapi_v3_params`; `test_parse_introspection`; `test_probe_ssr_finds_unlinked` (after passive probe on SSR: routes include `/secret-report` (source `robots`), `/about` (source `sitemap`); endpoint `GET /api/internal/stats` source `openapi`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): passive sources and OpenAPI/GraphQL schema discovery`.

### Task 15: JS bundle and source-map mining

**Files:** Create `probe/bundles.py`, `tests/test_bundles.py`; Modify `probe/passive.py` (source maps), `cli.py`

**Interfaces:**
- `BundleFindings(api_paths: set[str], routes: set[str], graphql_ops: set[tuple[str,str]], absolute_urls: set[str], source_maps: set[str])`.
- `mine_bundle(js: str) -> BundleFindings` — pure function; patterns: quoted strings starting `/api/`, `/v\d+/`, `/graphql`, or containing `/` and passed to `fetch(`, `axios.(get|post|put|patch|delete)(`, `.open("METHOD",` ; route objects `path:\s*["']([^"']+)` and `<Route path=`; gql documents `(query|mutation|subscription)\s+(\w+)`; `//# sourceMappingURL=(\S+)`; absolute `https?://` URLs.
- `async probe_bundles(client, api, cfg, store, frontier, role) -> dict[str,int]` — collects script URLs from `exchanges` where content_type is JavaScript (and `<script src>` on crawled pages), fetches each once, mines; source maps fetched and their `sourcesContent` mined too; adds endpoints/routes with source `bundle`/`source_map`; out-of-scope absolute URLs → `external_links` only.
- CLI: `appmap probe --phase passive` runs passive + schemas + bundles (each gated by `cfg.discovery` flags).

- [ ] **Step 1: Tests** — `test_mine_fetch_and_axios`; `test_mine_route_objects`; `test_mine_gql_docs`; `test_spa_internal_flags_found` (`GET /api/internal/flags` endpoint exists with source `bundle`, zero exchanges before active probe).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): JS bundle and source map mining`.

### Task 16: API replay and cross-role replay

**Files:** Create `probe/replay.py`, `tests/test_replay.py`; Modify `cli.py` (`probe --phase active`)

**Interfaces:**
- `async api_replay(client, sessions, cfg, store, queue, role: str) -> dict[str,int]` — for each endpoint with ≥1 exchange for `role`: OPTIONS and HEAD once; for GETs, variations (each optional query param omitted once; `page=2`, `offset=10`, `limit=5` when such params exist; path IDs substituted with up to 3 IDs seen in other responses). For endpoints with zero exchanges (schema/bundle only): GET with schema-derived or generated params; non-GET built from schema body or empty JSON. Every non-GET goes through `classify(origin="replay"|"schema")` and is enqueued if held. Login endpoints (`/login`, endpoints of `login` exchanges) are excluded. Origin recorded on exchanges.
- `classify_outcome(baseline_status: int, baseline_shape: dict|None, resp_status: int, resp_url: str, resp_shape: dict|None, login_url: str|None) -> str` → `allowed | redirect_login | forbidden | not_found | different_content | error`.
- `async cross_role_replay(client, sessions, cfg, store, queue) -> dict[str,int]` — for every route (GET page fetch) and every endpoint's representative exchange, send it as every **other** role via that role's `context.request` plus learned `auth_headers`; body = the captured request body; `classify(origin="cross_role")` (so POST/PUT/PATCH go out, DELETE/destructive enqueued); write `role_access` for all roles, including the discovering role (`allowed` from its own exchange) and `not_tested` for held ones.
- `async forced_browse(client, api, cfg, store, frontier, role) -> dict[str,int]` — only when `cfg.discovery.forced_browsing` (Task 2 already rejects it in production): built-in `resources/wordlist.txt` (~150 common paths: admin, backup, debug, `.git/HEAD`, `.env`, `server-status`, `actuator`, `api/v1`, …) plus `cfg.discovery.wordlists` files, GET each under every base URL; non-404 results → routes/endpoints with source `wordlist`, pushed to frontier.
- CLI: `appmap probe --run R --phase active [--budget-minutes 8]` (replay, forced browsing if enabled, then cross-role replay last so it covers everything found).

- [ ] **Step 1: Tests** — `test_classify_outcome` table; `test_alice_admin_access` (SSR: `role_access` for route `/admin` → admin `allowed`, alice `forbidden`, anonymous `redirect_login`); `test_cross_role_put_not_held_delete_held` (SPA: PUT replay exchange exists with origin `cross_role`; GraphQL `DeleteItem` mutation and any DELETE are in `approvals`); `test_internal_flags_exercised` (bundle-only endpoint now has an exchange with origin `schema`); `test_production_replay_post_held` (production cfg: non-GET `replay` POST enqueued as `data_create`); `test_forced_browse_finds_secret_report` (staging, flag on, robots disabled → `/secret-report` source `wordlist`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): API replay variations and cross-role access matrix`.

### Task 17: Approvals command and approved execution

**Files:** Modify `cli.py`, `crawl.py`, `approvals.py`; Create `tests/test_approvals_cli.py`

**Interfaces:**
- `appmap approvals --run R list` (prints pending items as JSON lines), `appmap approvals --run R write` (refresh `approvals.yaml` from store, preserving existing decisions/notes), `appmap approvals --run R apply` (`load_decisions`; status counts).
- `crawl --approved-only`: for each `queue.approved_unexecuted()`: restore `action_path`, perform the held action (accept `confirm` dialogs), `emit("approval_executed")`, `mark_executed`, record transition/exchanges with origin `approved`, push newly discovered surface into the frontier. Direct-request approvals (from replay) are sent via `DirectClient` with origin `approved`.

- [ ] **Step 1: Tests** — `test_apply_rule_and_execute` (SSR: rule approving `delete` with `label_contains: "Delete"` → after `--approved-only` a `POST /projects/{id}/delete` exchange with origin `approved` exists; logout item still pending); `test_write_preserves_decisions`; `test_denied_items_reported` (`status` counts `denied`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): approvals CLI and approved-action execution`.

---

## Milestone D — Map output and query

### Task 18: Coverage gaps and status

**Files:** Create `gaps.py`, `tests/test_gaps.py`; Modify `cli.py` (`status`)

**Interfaces:**
- `Gaps(unexercised_endpoints: list[dict], unsuccessful_forms: list[dict], unreached_routes: list[dict], unexplored_actions: list[dict], pending_approvals: list[dict], denied_approvals: list[dict])`; `compute_gaps(store) -> Gaps`.
- Pass tracking: `crawl` and `probe` record `meta.pass_<n>_new = {"states":..,"endpoints":..,"routes":..}`; `appmap status --run R [--gaps] [--new-pass]` — `--new-pass` increments `meta.pass` and snapshots counts; status JSON `counts` includes `pass`, `new_in_pass`.

- [ ] **Step 1: Tests** — `test_gaps_lists_schema_only_endpoint`; `test_failed_form_listed`; `test_new_in_pass_zero_when_nothing_new`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): coverage gaps and pass tracking`.

### Task 19: Map building — flows, value links, exports, APP-MAP.md

**Files:** Create `build.py`, `templates/app-map.md.j2`, `resources/SCHEMA.md`, `tests/test_build.py`; Modify `cli.py` (`build`)

**Interfaces:**
- `derive_flows(store) -> int` — one flow per successful form submission and per executed approval: steps = transitions along the `action_path` that reached the form + submit transition + its exchanges; consecutive submits within one `caused_by` chain merge into one multi-step flow (wizards). Default name `"<role>: <form label or action label> @ <route_pattern>"`.
- `derive_value_links(store) -> int` — for each JSON response, leaf values that are ints ≥ 1 or strings ≥ 4 chars that are not booleans/common words; link to later exchanges by the same role whose URL path, query, or body contains the value (`consumer_location` ∈ `path|query|body|header`).
- `derive_preconditions(store) -> int` — per flow: `{"role": r, "requires": [{"endpoint": ..., "json_path": ...}]}` for value links whose producer is outside the flow.
- `emit_naming_stub(store, path: Path) -> None` writes `names.yaml` (`flows: [{id, name, description: ""}]`, `forms: [{id, label, purpose: ""}]`); `apply_names(store, path) -> int`.
- `export_json(store, path) -> None` — `{"schema_version", "run": run.json, "<table>": [...]}` for every table except `frontier`.
- `render_app_map(store, rd, template) -> str` with sections in this order: handling notice (if `contains_production_data`), Run summary, Route tree, Endpoint inventory (grouped by first path segment), Role access matrix, Key flows, Forms, GraphQL operations, Unexercised surface (pending/denied approvals), Coverage gaps, Observations, Records created by this run (2xx non-GET exchanges whose request contains the marker), Where to dig deeper (sqlite + SCHEMA.md + example queries).
- CLI: `appmap build --run R [--names names.yaml]` — derives (idempotently: clears and rebuilds `flows`, `flow_steps`, `value_links`), writes `names.yaml` stub if absent, applies names if given, writes `appmap.json`, `APP-MAP.md`, copies `resources/SCHEMA.md` → run dir `SCHEMA.md`, sets `run.json` status `complete`.
- `resources/SCHEMA.md`: every table and column from Task 4 with meaning, the `sources`/`origin`/`outcome` vocabularies, and the recipes from Task 20.

- [ ] **Step 1: Tests** — `test_flow_for_new_project` (SSR: a flow whose steps end in `POST /projects/new` exchange); `test_value_link_created_id_used` (SPA: `POST /api/items` response `id` → later `GET /api/items/{id}` linked); `test_build_idempotent` (run twice → same flow count); `test_app_map_sections_present`; `test_json_export_round_trip` (table names == store tables minus frontier).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): flow derivation, value links, JSON and Markdown map`.

### Task 20: Query command

**Files:** Create `query.py`, `tests/test_query.py`; Modify `cli.py` (`query`)

**Interfaces:**
- `RECIPES: dict[str, str]` (SQL with named params) and `run_recipe(store, name: str, **params) -> list[dict]`; `run_sql(store, sql: str) -> list[dict]` (read-only store; rejects anything but a single `SELECT`/`WITH`).
- Recipes: `flows --role R`; `forms-posting-to --endpoint PATTERN`; `hidden-but-reachable --role R` (endpoints/routes with no `ui` source exchange for R but `role_access` R = `allowed`); `endpoint-shape --method M --path P` (params + response shapes per role + sample exchange ids); `path-to-state --state ID` (action path with locator sets); `role-matrix`; `gaps`.
- CLI: `appmap query --run R <recipe> [args] [--format json|table]`, `appmap query --run R sql "<SELECT …>"`.

- [ ] **Step 1: Tests** — `test_hidden_but_reachable` (SSR alice: `/secret-report` appears); `test_sql_rejects_writes` (`DELETE FROM states` → error); `test_path_to_state_has_locators`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): query recipes and read-only SQL`.

---

## Milestone E — Skills, packaging, acceptance

### Task 21: Skills, commands, manifests, validator

**Files:** Create under `skills/`: `map-running-application/SKILL.md` + `references/{methodology.md,config-reference.md,safety.md}`; `configure-crawl/SKILL.md`; `authenticate-roles/SKILL.md`; `crawl-application/SKILL.md` + `references/{spa-techniques.md,form-filling.md}`; `probe-endpoints/SKILL.md` + `references/discovery-sources.md`; `review-approvals/SKILL.md` + `references/destructive-heuristics.md`; `build-app-map/SKILL.md` + `references/app-map-template.md`; `query-app-map/SKILL.md` + `references/{schema.md,example-queries.md}`. Create `commands/appmap.md`, `commands/appmap-resume.md`, `.claude-plugin/plugin.json`, `absolute-skills/.claude-plugin/marketplace.json`, `AGENTS.md`, `README.md`, `LICENSE`, `scripts/validate.py`, `tests/test_validate.py`.

**Interfaces:**
- Each phase skill = exactly the CLI calls of its phase, what to read in the `APPMAP_STATUS` line, and the judgment it owns (per the spec's Division of Labor table):
  - `configure-crawl`: interview operator → write `appmap.config.yaml` + secrets env instructions; **must** get explicit authorization + scope confirmation; `appmap init`.
  - `authenticate-roles`: `appmap auth`; on `manual` login, tell operator what to do; verify each role `auth_ok`.
  - `crawl-application`: `appmap crawl` loop — rerun while `status == "in_progress"`; on `paused_circuit` stop and tell the operator; write `fixtures.yaml` when gaps show repeated validation failures.
  - `probe-endpoints`: `appmap probe --phase passive` before crawl, `--phase active` after.
  - `review-approvals`: `appmap approvals write`, read `approvals.yaml`, confirm/downgrade/add with reasons, present batch to human grouped by category, record decisions (never decide for the human), `apply`, `crawl --approved-only`.
  - `build-app-map`: `appmap build` → fill `names.yaml` (flow names/descriptions, form purposes) → `appmap build --names` → review `APP-MAP.md`.
  - `query-app-map`: orientation (`run.json`, `APP-MAP.md`), recipes, raw SQL, drilling to `events.jsonl`/HAR; notes that run dirs may hold production customer data.
  - Orchestrator `map-running-application`: Steps 0–8 from the spec's "Orchestrator Flow", double-back rule (stop when `new_in_pass` all zero, after 3 passes, or limits).
- `skills/query-app-map/references/schema.md` is byte-identical to `lib/appmap/resources/SCHEMA.md`.
- Commands: `/appmap <config.yaml | base URL>` and `/appmap-resume <run-dir>` invoke the orchestrator (`$ARGUMENTS`).
- `plugin.json`: name, version, description, author, homepage `https://github.com/absoluteappsec/absolute-skills`, license, keywords (`security, appsec, crawler, playwright, dast, attack-surface, testing`).
- `marketplace.json` (absolute-skills root): owner Absolute AppSec; plugins `[{"name":"absolute-appmap","source":"./dynamic/absolute-appmap", "description": …}]`.
- `AGENTS.md`: for non-Claude harnesses — what the plugin does, "start with `skills/map-running-application/SKILL.md`", the `scripts/appmap` entry point, `APPMAP_STATUS` contract, safety rules (authorization, approvals).
- `validate.py`: codereview's checks (manifest keys, frontmatter, name==dir, description ≤1024, referenced `references/*.md` exist) **plus**: description starts with "Use when"/"Use during"; every `appmap <subcommand>` mentioned in a SKILL.md is a registered CLI subcommand; no SKILL.md mentions `mcp__`; schema.md copy identical.

- [ ] **Step 1: Tests** — `test_validate_passes_on_repo` (`python scripts/validate.py` exit 0); `test_validate_catches_unknown_subcommand` (temp copy with `appmap explode` → exit 1); `test_validate_catches_mcp_reference`.
- [ ] **Step 2:** Write skills/commands/manifests/docs. **Step 3:** `python scripts/validate.py` → `OK: validated 8 skill(s) and plugin.json`; `pytest tests/test_validate.py -v` → PASS.
- [ ] **Step 4:** Commit `feat(appmap): skills, commands, manifests, validator`.

### Task 22: End-to-end acceptance and usage docs

**Files:** Create `tests/test_e2e.py`, `docs/usage.md`

**Interfaces:**
- Consumes the CLI only (subprocess `scripts/appmap` with `--dev` venv), exactly as an agent would.

- [ ] **Step 1: Tests** — for each fixture app, run the documented sequence: `init → auth → probe passive → crawl (loop while in_progress) → probe active → approvals write → (test writes decisions: approve deletes on marker records, deny logout) → approvals apply → crawl --approved-only → status --gaps --new-pass → build`. Assert:
  - every command's last line parses as `APPMAP_STATUS` JSON with `ok: true`;
  - SSR: routes ⊇ `{/, /projects, /projects/{id}, /projects/new, /admin, /account/password, /about, /secret-report}`; `role_access(/admin)` admin=allowed, alice=forbidden; ≥1 flow; `APP-MAP.md`, `appmap.json`, `SCHEMA.md` exist;
  - SPA: spa_routes ⊇ `{/, /items, /items/{id}, /settings, /admin}`; endpoints include `graphql_op ListItems`, `graphql_op DeleteItem` (held, then executed only if approved), `sse /api/events`, `/api/internal/flags`; a `value_links` row from `POST /api/items` to `GET /api/items/{id}`; `locators` rows exist for every transition;
  - **Review Focus 3:** recursive scan of the whole run dir (all files, incl. HAR, artifacts, bodies, sqlite bytes) finds none of `admin-pw-Fx1`, `alice-pw-Fx2`, any raw `sid` cookie value, or the SPA JWT;
  - no exchange URL host outside allow_hosts.
- [ ] **Step 2:** Run `pytest tests/test_e2e.py -v` → fix integration bugs until PASS.
- [ ] **Step 3:** Write `docs/usage.md` (install, config example from spec, the command sequence above, output layout, querying).
- [ ] **Step 4:** Run full suite `pytest -v` and `python scripts/validate.py` → all PASS.
- [ ] **Step 5:** Commit `test(appmap): end-to-end acceptance against fixture apps; usage docs`.
