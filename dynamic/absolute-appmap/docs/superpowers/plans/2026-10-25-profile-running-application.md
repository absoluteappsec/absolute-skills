# profile-running-application Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `appmap profile` (deterministic evidence extraction → `profile.json`) and the `profile-running-application` skill (agent narrative → `APP-PROFILE.md`) to the `absolute-appmap` plugin, producing a descriptive application briefing for QA testers/developers from app-map run directories only.

**Architecture:** A new `lib/appmap/profile/` package reads one or more run directories read-only (store + `bodies/` + `run.json`), extracts evidence, runs data-driven technology fingerprint rules, computes descriptive sections (layout, features, data model, data setup, accounts, environments, coverage), applies agent-written names from `profile-names.yaml`, and renders `profile.json` + `APP-PROFILE.md` via Jinja2. A new SKILL.md drives the agent through run → judge → name → re-render.

**Tech Stack:** Python 3.11+, existing deps only (PyYAML, Jinja2, sqlite3); pytest.

**Spec:** `dynamic/absolute-appmap/docs/superpowers/specs/2026-10-25-profile-running-application-design.md` — read it alongside this plan (especially "Resolved Details").

## Global Constraints

- Plugin root `dynamic/absolute-appmap/`; all paths below relative to it.
- Read-only on inputs: never write to a run's `appmap.sqlite` (open with `Store.open_readonly`), never contact the target, never read `appmap.secrets.env` or env var values.
- Descriptive only: no outdated/vulnerable/CVE/risk wording in code-generated text.
- `PROFILE_SCHEMA_VERSION = "1.0.0"`.
- Confidence vocabulary exactly `confirmed | likely | guessed` with the rules in the spec's Resolved Details.
- Evidence ref shape exactly `{"run_id": str, "kind": "exchange"|"state"|"file"|"route"|"endpoint"|"flow"|"form", "id": str|int}`; snippets ≤ 200 chars, taken from stored (already-redacted) data and passed through `Redactor` again.
- Production masking: if any input run has `contains_production_data`, observed usernames/emails are masked (`mask_identity`) unless `--include-observed-accounts`.
- CLI: `appmap profile --run R [--run R2 ...] [--out DIR] [--include-observed-accounts]`; last stdout line `APPMAP_STATUS` with `command: "profile"`, statuses `profiled | partial | error`.
- Outputs: `profile.json`, `APP-PROFILE.md`, `profile-names.yaml` in the first run dir (or `--out`).
- Per-body scan cap 2 MiB; total evidence scan must finish in < 60 s on a 10k-exchange run.
- Skill conventions as the existing plugin (frontmatter name == dir, description ≤1024 starting "Use when/Use during", Purpose → Step N → Completion Criteria → Reference Files; validate.py must pass).
- Commits end with the Co-Authored-By line from the committer's session.

## Review Focus

1. **Secrets in the profile.** A snippet taken from a body/header could carry a token the run-time redaction missed. Expected: every snippet re-redacted; `profile.json`/`APP-PROFILE.md` never contain fixture passwords, sid, JWT, CSRF values. Test in Task 7.
2. **False version claims.** Strings like `v1.2` or `2.0` anywhere in a bundle must not become library versions. Expected: version patterns anchored to banner/header/path forms; a random `version: "2"` in app code doesn't produce a framework version. Test in Task 2.
3. **Huge inputs.** 50 MB bundles / 10k exchanges. Expected: per-body 2 MiB cap, no crash, bounded time. Test in Task 1.
4. **Partial runs.** No `build` yet, failed roles, no schema. Expected: status `partial`, "Could not determine" entries, no exception. Test in Task 6.
5. **Re-run name stability.** Agent names survive a re-run; vanished feature keys go to `stale:`. Test in Task 5.

---

## File Structure

```
lib/appmap/profile/__init__.py      # PROFILE_SCHEMA_VERSION, build_profile()
lib/appmap/profile/evidence.py      # Evidence dataclass, iter_evidence()
lib/appmap/profile/fingerprint.py   # Rule, load_rules(), detect()
lib/appmap/profile/sections.py      # layout, data_model, features, data_setup, accounts, environment, coverage
lib/appmap/profile/multirun.py      # merge per-run profiles, differences
lib/appmap/profile/names.py         # profile-names.yaml stub/apply (stable keys, stale)
lib/appmap/profile/render.py        # render_app_profile()
lib/appmap/resources/fingerprints.yaml
lib/appmap/templates/app-profile.md.j2
lib/appmap/cli.py                   # + profile subcommand
skills/profile-running-application/SKILL.md + references/{profile-guide.md,fingerprints.md}
tests/data/fingerprints/*           # captured header/html/bundle samples
tests/test_profile_*.py
```

---

### Task 1: Evidence extraction

**Files:** Create `lib/appmap/profile/__init__.py` (version constant only for now), `lib/appmap/profile/evidence.py`, `tests/test_profile_evidence.py`

**Interfaces:**
- `Evidence(run_id: str, kind: Literal["header","cookie","html","script","source_map","schema","spa","error_page","auth"], ref: dict, url: str, text: str, headers: dict[str,str])` (frozen dataclass; `text` capped at 2 MiB).
- `iter_evidence(run_dir: Path) -> Iterator[Evidence]` — opens the store read-only; yields: per exchange, one `header` evidence (response headers) and one `cookie` evidence per Set-Cookie *name* (value never included); `html`/`error_page` (status ≥400 or known error markers) for HTML bodies; `script` for JS bodies; `source_map` for `.map` bodies (the `sources` list joined + `sourcesContent` heads); `schema` for exchanges recorded as OpenAPI/GraphQL schema fetches; one `spa` evidence from SPA detection meta/events if recorded; `auth` from roles + login exchanges (shape only: form fields names, token header *names*). Bodies come from inline `resp_body` or `bodies/` path.

- [ ] **Step 1: Tests** — `test_iter_evidence_kinds` (synthetic store with one HTML, one JS, one .map, one 404 Django debug page, Set-Cookie `csrftoken=…; sessionid=…` → kinds present; cookie evidence texts are exactly `csrftoken`, `sessionid`; no cookie values anywhere in any evidence); `test_body_cap` (a 50 MB JS file → text length ≤ 2 MiB, completes < 2 s); `test_readonly` (store file mtime unchanged after iteration).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(appmap): profile evidence extraction`.

### Task 2: Fingerprint rules and engine

**Files:** Create `lib/appmap/resources/fingerprints.yaml`, `lib/appmap/profile/fingerprint.py`, `tests/data/fingerprints/` samples, `tests/test_profile_fingerprint.py`

**Interfaces:**
- YAML rule shape: `{tech, category, evidence: [kinds], match: regex, version: regex|null (one capture group), confidence: confirmed|likely, implies: [{tech, category}]|null}`. Categories: `language, server_framework, frontend_framework, js_library, css_framework, web_server, app_server, cdn_proxy, auth, api_style, third_party`.
- `Rule` dataclass; `load_rules(path: Path | None = None) -> list[Rule]` (default packaged file; validates regexes compile and categories are known).
- `Technology(name, category, versions: list[str], confidence, evidence: list[dict])`; `detect(evidence: Iterable[Evidence], rules: list[Rule]) -> list[Technology]` — confidence: rule's level; two distinct rules agreeing on the same tech → `confirmed`; `implies` adds the implied tech as `likely` (e.g. Django → Python); conflicting versions all listed sorted; evidence entries `{ref, kind, snippet}` (≤3 per technology, snippets ≤200 chars).
- Initial rule coverage (minimum): Django (csrftoken/sessionid cookies, csrfmiddlewaretoken, DEBUG page incl. version line, `/admin/` login title), Django REST Framework / drf-spectacular (schema info, browsable API markers), Flask/Werkzeug (Server header, debugger page), Rails, Laravel, Express (`X-Powered-By: Express`, connect.sid), Spring (Whitelabel), ASP.NET (headers, cookie), PHP (`X-Powered-By: PHP/x`), nginx/Apache/IIS/gunicorn/uvicorn (`Server`), Cloudflare/CloudFront/Fastly (headers), React, Vue (incl. `vue.global.prod.js` banner `vue v3.4.38`), vue-router (banner), Angular (`ng-version`), Next.js, Nuxt, SvelteKit, jQuery (banner `jQuery v`), Bootstrap (banner/CSS), Font Awesome, Google Analytics/Tag Manager, JWT bearer (auth header name), cookie session, CSRF token mechanism, OpenAPI/GraphQL API styles.

- [ ] **Step 1: Tests** — `test_rules_load_and_validate`; one parametrized test per sample file in `tests/data/fingerprints/` (`expected.yaml` maps sample → [(tech, version, confidence)]); `test_two_rules_confirm`; `test_implies_language_likely` (Django → Python likely); `test_no_false_version_from_app_code` (bundle containing `const version = "2"; var v="1.2"` and no banner → no js_library versions); `test_vue_fixture_banner` (vendored fixture file → Vue 3.4.38, vue-router 4.4.3, confirmed).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): data-driven technology fingerprinting`.

### Task 3: Descriptive sections I — layout, data model & types, environment

**Files:** Create `lib/appmap/profile/sections.py`, `tests/test_profile_sections.py`

**Interfaces:**
- `layout(store, run_id) -> dict`: `{"areas": [{prefix, routes:[pattern], endpoints:[method path_pattern], roles:[...]}], "entry_points": {role: [url]}, "api_groups": [{resource, endpoints:[...]}]}` — areas by first path segment (and second for `/api/...`), entry points = each role's first visited states, API groups by resource segment.
- `data_model(store, run_id) -> list[dict]`: entities `{name, source: openapi|graphql|inferred, fields:[{name, type, required, constraints, enum, example_redacted}], id_format: int|uuid|slug|unknown, endpoints:[...], evidence:[ref]}` — from OpenAPI/GraphQL schema exchanges when present; else from `responses.body_shape` + `params` + `fields`; id format from path params.
- `environment(run_dir) -> dict`: `{run_id, base_urls, hosts, profile, started_at, tool_version, contains_production_data}` from `run.json`.

- [ ] **Step 1: Tests** — on a built SSR fixture run (reuse helpers from `tests/test_build.py`/`test_e2e.py`): areas include `/projects`, `/admin`, `/api`; API group `projects`; entity `projects` inferred with id_format int; with a stub OpenAPI exchange → entity fields with types from the schema; environment fields match `run.json`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): profile layout, data model and environment sections`.

### Task 4: Descriptive sections II — features, data setup, accounts, coverage

**Files:** Modify `lib/appmap/profile/sections.py`; extend `tests/test_profile_sections.py`

**Interfaces:**
- `features(store, run_id) -> list[dict]`: candidates `{key, resource, flow_keys:[...], routes, endpoints, forms, roles, name_suggestion}` — cluster flows/forms/endpoints by primary resource (first non-api path segment after normalizing); `key = sha1(resource + "|" + "|".join(sorted(flow_keys)))[:12]` prefixed `ft-`.
- `data_setup(store, run_id) -> dict`: `{"preconditions": [{flow_key, requires:[...]}], "created_records": [...] (from build's records list logic — import, don't duplicate), "fixtures_used": bool, "seed_observations": [{resource, id_min, id_max, count}]}`.
- `mask_identity(s: str) -> str` (`chris@example.com` → `c***@example.com`; `chris` → `c***`).
- `accounts(store, run_id, *, mask: bool) -> dict`: `{"roles": [{name, login_type, auth_ok, reachable_routes:int, reachable_endpoints:int}], "created_by_run": [...], "observed": [{value, where: ref}]}` — observed usernames/emails from response JSON keys (`username`, `email`, `user`, `login`) and HTML `mailto:`; capped 50; masked when `mask`. Never passwords/tokens/env values.
- `coverage(store, run_id) -> dict`: `{"features_x_roles": {feature_key: {role: outcome_summary}}, "unexercised": compute_gaps(...) summary, "approvals": {pending, denied, skipped}}`.

- [ ] **Step 1: Tests** — SSR run: a `projects` feature containing the create flow; data_setup lists the create precondition and created records; accounts lists admin/alice/anonymous with login types and no password strings; `mask_identity` table; masking applied when `mask=True`; coverage grid has admin `allowed` / alice `forbidden` for the admin feature.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): profile features, data setup, accounts and coverage`.

### Task 5: Profile assembly, multi-run, names

**Files:** Modify `lib/appmap/profile/__init__.py`; create `profile/multirun.py`, `profile/names.py`, `tests/test_profile_assembly.py`

**Interfaces:**
- `build_profile(run_dirs: list[Path], *, include_observed_accounts: bool = False) -> dict` → `{"profile_schema_version", "generated_at", "status": "profiled"|"partial", "warnings": [...], "could_not_determine": [...], "environments": [...], "technologies": [...], "layout", "features", "data_model", "data_setup", "accounts", "coverage", "differences": {...}|None, "purpose": None, "feature_names": {}}`. Partial when: no flows (build not run), any role auth_ok false, no schema found (warning only), any run crawl status not done.
- `multirun.merge(per_run: list[dict]) -> dict` — technologies joined by name+category (versions per environment), routes by pattern, endpoints by method+path_pattern, role-access summary; warn when route-set overlap < 30%.
- `names.emit_profile_names(profile, path)`: stub `{purpose: "", features: [{key, name, description: "", how_it_works: ""}], stale: []}`; preserves existing entries; moves entries whose key vanished to `stale`.
- `names.apply_profile_names(profile, path) -> int` — fills `purpose`, `feature_names[key] = {name, description, how_it_works}`; ignores malformed entries.

- [ ] **Step 1: Tests** — two SSR runs (one with a modified fixture route) → differences list the route; overlap warning on unrelated runs; partial status on an unbuilt run with `could_not_determine` entries; names survive re-run; vanished key → `stale`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): profile assembly, multi-run differences, names`.

### Task 6: Rendering and `appmap profile` CLI

**Files:** Create `profile/render.py`, `templates/app-profile.md.j2`; modify `cli.py`; create `tests/test_profile_cli.py`

**Interfaces:**
- `render_app_profile(profile: dict) -> str` — section order: data-handling notice (if production) → Purpose → Main features (name, description, roles, entry points) → How it works (per feature) → Technology stack (table: name, category, version(s), confidence, evidence count) → Layout → Data model & types → Data setup → Test accounts → Environments (+ differences) → Coverage orientation → Could not determine → Where to dig deeper. Long tables capped with "N more — see profile.json".
- CLI `profile`: opens runs read-only; writes `profile.json` (atomic), `profile-names.yaml` (stub/preserve), applies names, writes `APP-PROFILE.md`; status `profiled|partial` (ok true) or `error` (ok false: missing appmap.sqlite, unwritable out dir without `--out`); `next` hints: when purpose/feature names empty → "fill profile-names.yaml, then re-run appmap profile"; counts: technologies, features, entities, environments.
- Run lock: profile is read-only on inputs → does not take the run lock (writes only its own files atomically).

- [ ] **Step 1: Tests** — CLI on a built SSR run: status profiled, files exist, APPMAP_STATUS last line, sections in order; `--run` twice → environments 2; missing sqlite → error; production run → notice + masked accounts, `--include-observed-accounts` unmasks; re-run keeps names.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(appmap): appmap profile command and APP-PROFILE.md`.

### Task 7: Skill, docs, and acceptance

**Files:** Create `skills/profile-running-application/SKILL.md`, `references/profile-guide.md` (confidence rules, evidence-citing format for prose, how to infer purpose/features, what never to write), `references/fingerprints.md` (rule format, how to add a detector); modify `skills/query-app-map/SKILL.md` (point to the profile first), `skills/map-running-application/SKILL.md` + `references/methodology.md` (optional final step + `profile` statuses), `AGENTS.md`, `README.md` (skills table, outputs), `docs/usage.md`, `lib/appmap/resources/SCHEMA.md` + its byte-identical copy (document profile.json shape briefly); `scripts/validate.py` only if needed; create `tests/test_profile_e2e.py`.

- Skill steps (per spec "Skill flow"): verify inputs (`appmap status`), run `appmap profile`, read profile.json + APP-MAP.md, judge (purpose, features, how it works — every factual sentence cites refs; may downgrade confidence, never upgrade without evidence), fill `profile-names.yaml`, re-run, report summary + "could not determine". Completion checklist includes "no outdated/vulnerable judgments written".

- [ ] **Step 1: Tests (`test_profile_e2e.py`)** — build SSR and SPA fixture runs via the documented CLI sequence (reuse e2e helpers), then `appmap profile`: SSR → Werkzeug/Flask + Python (likely), cookie-session auth, CSRF mechanism; SPA → Vue 3.4.38 + vue-router 4.4.3 confirmed, bearer JWT auth style, GraphQL API style; secret scan over profile.json/APP-PROFILE.md/profile-names.yaml for fixture passwords, sid values, JWTs, CSRF values (reuse e2e scanner incl. base64 forms) → zero hits; no words "outdated", "vulnerable", "CVE" in generated output.
- [ ] **Step 2:** Write skill + docs; `python3 scripts/validate.py` → OK.
- [ ] **Step 3:** Full suite `.venv/bin/pytest -q` → PASS.
- [ ] **Step 4:** Field check (not a unit test): run `appmap profile` on the VTM run dir under `/Users/seth/Documents/Absolute AppSec/appmap-runs/vtm/` (read-only on it; `--out` to a scratch dir) and record detected technologies in the report — expect Django, DRF/drf-spectacular, Python.
- [ ] **Step 5:** Commit `feat(appmap): profile-running-application skill, docs, acceptance`.
