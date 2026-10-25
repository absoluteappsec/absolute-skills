# Design Spec: `profile-running-application` skill + `appmap profile`

**Date:** 2026-10-25
**Author:** Seth Law (Redpoint Security / Absolute AppSec)
**Status:** Draft — pending approval
**Location:** `absolute-skills/dynamic/absolute-appmap/` (new skill in the existing plugin)

## Summary

A skill that turns a finished `absolute-appmap` run into an **application profile**: a briefing that
brings a QA tester or developer who has never seen the application up to speed — what it is for, its
main features, how it is built (language, framework, libraries, versions), how it is laid out, how
each feature works, and everything they need to set up data and accounts — all pointed toward
building **full test coverage**.

It is **dynamic only**: it uses nothing but what the running application revealed, as captured in
the app-map run directory. No source code.

## Intent and the Why

The app map (`APP-MAP.md`, `appmap.sqlite`) is complete but raw: routes, endpoints, flows and a role
matrix. A person (or a later skill) starting test work needs context first — "what is this thing,
what does it do, what is it built with, what do I need to test it?" The profile is that context
layer. It sits between mapping and test generation: the future test-generation skill and humans both
read it to plan coverage.

## Goals

- Answer, from map evidence, everything a tester/developer needs to come up to speed:
  1. **Purpose** — what the application is for and who uses it (inferred).
  2. **Main features** — what it does, each tied to its flows, pages, endpoints and roles.
  3. **Technology** — language, server framework, front-end framework, supporting libraries,
     web/app server, CDN/hosting hints, auth mechanism, API styles — **with versions when observable**.
  4. **Layout** — areas/sections, page hierarchy, API surface grouped by resource, how UI and API
     connect.
  5. **How features work** — for each feature: entry point, pages, forms, API calls in order, data
     passed between steps.
  6. **Data model & types** — resources/entities inferred from API schemas, response shapes and form
     fields; field names, types, constraints, enums, required-ness, id formats.
  7. **Data setup** — what data each flow needs to exist first (preconditions, value links), what the
     crawl created (marker records), fixtures used, seed-data observations (e.g. ids 1–100 present).
  8. **Test accounts** — configured roles (name, login type, what each could reach), accounts the run
     created, other usernames/accounts observed in responses. **Never passwords or tokens.**
  9. **Environments** — which environment(s) the evidence comes from: base URLs, hosts, profile
     (staging/production), run dates, per-environment differences when several runs are given.
  10. **Coverage orientation** — features × roles grid, entry points, unexercised/held surface
      (pending/denied approvals, coverage gaps) so testers know what the map did *not* see.
- Every factual claim carries **evidence** (pointers to exchanges/states/events) and a
  **confidence** (confirmed / likely / guessed).
- Repeatable: the evidence extraction is deterministic code; re-running after a new crawl updates the
  profile.
- Portable like the rest of the plugin: any agent, any model, no MCP.

## Non-Goals

- **No analysis or judgment**: no "outdated", "vulnerable", "insecure", CVE lookups, or risk ratings.
  Descriptive only — later skills/tools analyze.
- **No source code** input (dynamic only, for now).
- No new crawling or requests to the target. The profile reads the run directory only.
- No test generation (a later skill).

## Anti-Goals

- Stating guesses as facts (every claim must show evidence and confidence).
- Leaking credentials/session tokens into the profile (the run dir is already redacted; the profile
  must not reintroduce anything — e.g. never copy `appmap.secrets.env` or env var values).
- A profile that only a human can read, or only a machine can read — it must be both.
- Burying the reader: the profile must lead with purpose and features, not a dump of headers.

## Constraints

- Lives in the existing `absolute-appmap` plugin; follows its conventions (SKILL.md format, `scripts/
  appmap` wrapper, `APPMAP_STATUS` last-line contract, `validate.py` checks).
- Name avoids collision with `absolute-codereview:profile-application` (static, source-based):
  skill `profile-running-application`, command `appmap profile`.
- Input: one or more completed run directories (one per environment). Output written into the
  (first) run directory, or a directory given with `--out`.
- Python 3.11+, existing dependencies only (PyYAML, Jinja2, sqlite3).

## Decisions Made in Conversation (fixed)

1. Audience: QA testers and developers coming up to speed; purpose = full test coverage.
2. Content order: purpose → main features → language/framework/libraries → layout → how it works;
   plus data setup, schema/types, identified test accounts, environments — "anything the skill can
   answer".
3. **Dynamic only** — no source code.
4. **Descriptive only** — no outdated/vulnerable flags; leave analysis to later skills.

## Design

### Two layers

1. **`appmap profile --run R [--run R2 ...] [--out DIR]`** — deterministic evidence extraction into
   `profile.json` (structured, schema-versioned). It never contacts the target.
2. **Skill `profile-running-application`** — the agent runs the command, reads `profile.json` plus
   `APP-MAP.md`/recipes, and writes the narrative `APP-PROFILE.md` (purpose, features, how it works,
   orientation), filling `profile.json`'s agent-owned fields (purpose, feature names/descriptions)
   through a `profile-names.yaml` stub, the same pattern as `build`'s `names.yaml`.

### Technology fingerprinting (code)

A rule table (data file, e.g. `resources/fingerprints.yaml`) of detectors, each: technology,
category (language, server framework, front-end framework, JS library, CSS framework, web server,
app server, CDN/proxy, auth, API style, analytics/3rd-party), evidence source, match pattern, and an
optional version-extraction pattern. Evidence sources available in the run dir:

- Response headers (`Server`, `X-Powered-By`, `Via`, `X-AspNet-Version`, framework-specific headers).
- Cookie names (`csrftoken`/`sessionid` → Django, `JSESSIONID` → Java servlet, `laravel_session`,
  `_rails_session`/`_session_id`, `connect.sid` → Express, `PHPSESSID`, `ASP.NET_SessionId`, …).
- Error and default pages (Django DEBUG pages incl. version line, Werkzeug, Rails, Laravel/Whoops,
  Spring Whitelabel, IIS/nginx/Apache defaults).
- HTML: `<meta name="generator">`, script/link URLs with versioned filenames, framework markers
  (`csrfmiddlewaretoken`, `data-reactroot`, `ng-version`, `__NEXT_DATA__`, `__NUXT__`, Vue/Svelte
  markers), admin consoles (`/admin/` Django admin).
- JS bundles and source maps (from the map's bundle mining): library banners
  (`/*! jQuery v3.6.0`), `node_modules/<pkg>/` paths in source maps, `version:` constants.
- SPA detection already in the store (framework + router).
- API schemas captured (OpenAPI `info`, servers, DRF/drf-spectacular markers, GraphQL introspection).
- Auth observations: login form shape, token style (cookie session, bearer JWT — header only, never
  values), CSRF mechanism, SSO hosts (`login_allow_hosts`).

Each detected technology: `{name, category, version|null, confidence, evidence: [{kind, ref, snippet}]}`
where `ref` points at an exchange id / state id / file, and `snippet` is a short redacted excerpt.
Multiple detectors agreeing raise confidence; versions conflicting across evidence are all listed.
Language is inferred from the framework (Django → Python) and marked `likely`.

### Other sections (code extracts, agent narrates)

- **Layout**: route tree and endpoint groups (from the map), areas inferred by path prefix and nav
  structure (states + transitions), entry points per role.
- **Features**: candidate features clustered from flows, forms and resource groups (code proposes
  clusters by resource/path + flow; the agent names, merges and describes them).
- **Data model & types**: entities from OpenAPI schemas and GraphQL types when present; otherwise
  inferred from response `body_shape`s, `params` types/examples and form `fields` constraints; id
  formats (int, uuid, slug) from path patterns.
- **Data setup**: per flow, preconditions and value links (what must exist first), records created by
  the run, fixtures used, observed seed ranges.
- **Test accounts**: config roles (name, login type, auth outcome, reach from role_access), accounts
  created by the run (marker usernames/emails), usernames/emails observed in responses (listed
  as-is — customer data handling notice applies for production runs). No passwords, tokens, or
  env-var values.
- **Environments**: one block per run directory (base URLs, hosts, profile, run date, tool version);
  when several runs are given, a differences table (technologies/versions/routes present in one but
  not another).
- **Coverage orientation**: features × roles grid with access outcomes, unexercised surface, pending
  and denied approvals, coverage gaps from `status --gaps`.

### Outputs

- `profile.json` — structured, versioned (`profile_schema_version`), all sections above with evidence
  refs; agent-owned fields filled from `profile-names.yaml`.
- `APP-PROFILE.md` — the narrative briefing (Jinja2 template + agent-written prose), leading with
  purpose and features; tech stack table with versions/confidence/evidence; capped long tables with
  "see profile.json" pointers; data-handling notice when any input run is production.
- `profile-names.yaml` — stub for the agent's names/descriptions (survives re-runs, keyed stably).

### Skill flow (`profile-running-application`)

1. Confirm the run directory(ies) are complete (`appmap status`); if not, say what is missing.
2. Run `appmap profile`; read `profile.json` and `APP-MAP.md`.
3. Judgment (agent): infer purpose; name and describe features; write "how it works" per feature;
   check technology claims against evidence (downgrade confidence if weak — never upgrade without
   evidence).
4. Fill `profile-names.yaml`, re-run `appmap profile` to render `APP-PROFILE.md`.
5. Report: one-paragraph summary + path, and what the profile could not determine.

Also update `query-app-map` (and AGENTS.md/README) to point downstream agents at the profile first.

### Testing

- Unit tests per detector family with captured header/HTML/bundle samples (Django, Rails, Express,
  Laravel, Spring, ASP.NET, React/Vue/Angular/Next/Nuxt, jQuery/Bootstrap banners, source-map paths).
- End-to-end on both fixture apps (Flask SSR → Flask/Werkzeug + Python; Vue SPA → Vue 3.4.38 +
  vue-router 4.4.3 from vendored banners) asserting technologies, versions and evidence refs.
- Secret scan: `profile.json`/`APP-PROFILE.md` contain no fixture passwords/tokens.
- Field test on the VTM run (Django/DRF expected).


## Resolved Details (from the builder check)

- **Evidence availability (answered by the existing plugin):** the run's `exchanges` table keeps
  response headers (JSON, credential values redacted, cookie *names* preserved) and full bodies
  (inline or under `bodies/`), including JS bundles/source maps fetched by the crawl or bundle
  mining, error pages and schema documents. `appmap profile` reads only these. If a body is missing,
  the technology is still reported but without a version — **no re-fetching** (Claude's call).
- **Evidence refs (Claude's call):** `{run_id, kind: exchange|state|file|route|endpoint, id}`,
  always namespaced by `run_id`; resolvable with `query-app-map` against that run. Cross-run joins
  use technology `name+category`, routes by `pattern`, endpoints by `method+path_pattern`.
- **Stable feature keys (Claude's call):** a feature's key is its sorted set of member flow keys +
  primary resource prefix. On re-run, names whose key no longer exists are kept in a `stale:` block
  of `profile-names.yaml` and listed in the report for the agent to re-map — never silently dropped.
- **Confidence rules (Claude's call):** *confirmed* = an explicit name/version string from an
  authoritative source (header, banner, debug/error page, schema `info`, generator meta) or two
  independent detectors agreeing; *likely* = a single indirect marker (cookie name, DOM marker) or an
  inference (language from framework); *guessed* = agent inference without a detector. Agent-written
  prose must cite evidence refs for factual claims; `appmap profile` rejects technology entries
  without evidence and the skill's completion checklist requires refs in "how it works".
- **Incomplete inputs (Claude's call):** `appmap profile` runs on partial runs with warnings
  (status `partial`, listing what is missing: no build → no flows/value links, failed roles, no
  schema); APP-PROFILE.md gets a "Could not determine" section. It refuses only when there is no
  `appmap.sqlite`.
- **Multiple runs (Claude's call):** assumed to be the same application in different environments;
  warn when their route sets overlap < 30%. Differences cover technologies/versions, routes,
  endpoints, and a role-access summary. Output goes to the first run dir unless `--out`; if that dir
  is not writable, `--out` is required (status `error` with a clear next).
- **Observed accounts in a shareable document (Claude's call):** for staging runs, observed
  usernames/emails are listed as-is; for any production input run they are **masked** in
  `profile.json`/`APP-PROFILE.md` (e.g. `c***@example.com`) unless `--include-observed-accounts` is
  given, and the data-handling notice is shown. Config roles (names, login types) are always listed;
  passwords, tokens and env-var values never.
- **Tests:** fixture apps (Flask SSR, Vue SPA) are in the repo and the e2e builds runs from them;
  the VTM field-test run is available locally under `/Users/seth/Documents/Absolute AppSec/appmap-runs/vtm/`.
- **CLI/validator:** `appmap profile` is a normal subcommand (own `APPMAP_STATUS` command value,
  statuses `profiled|partial|error`, documented in methodology.md); validate.py discovers skills by
  directory, so no manifest list to extend.

## Left to the Builder

- The fingerprint rule format details and the initial detector list beyond those named above.
- Feature-clustering heuristic details.
- `profile.json` exact shape (within the sections above), template layout.

## Calls Claude Made (for review)

1. Skill name `profile-running-application`, command `appmap profile`, inside `absolute-appmap`.
2. Two layers: deterministic extraction (`profile.json`) + agent narrative (`APP-PROFILE.md`).
3. Fingerprints as a data file (extensible without code changes).
4. Confidence levels confirmed / likely / guessed, with evidence refs on every claim.
5. Multiple run directories → environments section with a differences table.
6. Observed usernames/emails listed as-is for staging, masked by default for production inputs; never credentials.
7. All items in "Resolved Details" marked as Claude's call.
