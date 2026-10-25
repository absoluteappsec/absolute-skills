# absolute-appmap schema reference (v1.0.0)

This file describes `appmap.sqlite` (and `appmap.json`, which carries the same public tables)
precisely enough to write SQL against a run directory without reading the crawler's code.
The schema version is in `meta.schema_version` (semver; a major bump means incompatible
changes, documented here).

Conventions:

- **Types** are SQLite storage types. `INTEGER` booleans are `1`/`0`; `NULL` means unknown or
  not applicable.
- **JSON** marks a `TEXT` column holding a `json.dumps` value. In SQL use `json_extract`,
  `json_each` or `json(...)`; in `appmap.json` these columns are already decoded.
- **Event ids** (`ev-000123`) in `*_event` / `event_id` columns point into `events.jsonl`
  (one JSON object per line: `id`, `ts`, `type`, `role`, `caused_by`, `data`).
- **State ids** are `st-` + 12 hex chars: a hash of role, route pattern and DOM fingerprint
  (a state is per role).
- **Route patterns** normalize path segments: digits → `{id}`, UUIDs → `{uuid}`, long hex →
  `{hash}`, segments carrying the run marker → `{slug}`, plus SPA router patterns
  (`/items/:id` style, recorded as found).
- **Redaction:** credentials and session tokens are replaced in every table by
  `<redacted:KIND>` (`password`, `secret`, `cookie`, `authorization`, `jwt`, `csrf`,
  `api_key`; `undecodable` replaces a binary body that could not be checked for secrets); everything else, including customer data, is kept. If `run.json` has
  `contains_production_data: true`, treat the run directory as sensitive production data.
- Paths in the run directory (`screenshot`, `dom_snapshot`, `*_body_path`) are relative to
  the run directory.

## Vocabularies

| Vocabulary | Values |
|---|---|
| `exchanges.origin` | `ui` (browser crawl), `replay` (direct API replay of captured requests), `cross_role` (another role's request replayed for the access matrix), `schema` (requests built from OpenAPI/GraphQL schemas), `passive` (robots, sitemaps, well-known files, bundles fetched directly), `bundle` (requests to URLs mined from JS bundles), `approved` (an approved held action executed), `login` (role authentication) |
| `sources.source` | `ui`, `spa_router`, `bundle`, `openapi`, `graphql`, `sitemap`, `robots`, `well_known`, `source_map`, `replay`, `wordlist` |
| `sources.item_kind`, `role_access.item_kind` | `route` (→ `routes.id`), `endpoint` (→ `endpoints.id`) |
| `role_access.outcome` | `allowed` (2xx, same JSON shape as the discovering role's response), `redirect_login` (401, or landed on the login page), `forbidden` (403), `not_found` (404/410), `different_content` (3xx, 400/422 — authorization passed but the input was rejected —, redirected to another path, or 2xx with a different JSON shape), `error` (405, 5xx, network failure), `not_tested` (held for approval, logout, or tied to an approval) |
| `routes.kind` | `page` (server-rendered or visited URL), `spa_route` (client-side router route) |
| `endpoints.kind` | `rest`, `graphql_op`, `websocket`, `sse`, `static`, `page` |
| `endpoints.graphql_type` | `query`, `mutation`, `subscription` (NULL unless `kind='graphql_op'`) |
| `params.location` | `path`, `query`, `body`, `header`, `cookie` |
| `locators.strategy` | `testid`, `role_name` (value `role\|accessible name`), `label`, `text`, `css`, `xpath`; lower `rank` = more stable |
| `transitions.action_type` | `navigate` (link or URL load), `click`, `submit` |
| `approvals.category` | `delete`, `credential_change`, `payment`, `outbound_message`, `deactivate`, `logout`, `data_create` |
| `approvals.decision` | `pending`, `approve`, `deny`, `skip` |
| `roles.login_type` | `form`, `manual`, `none`, `storage_state` |
| `observations.kind` | `server_error`, `rate_limited`, `stack_trace`, `debug_page`, `captcha` |
| `value_links.consumer_location` | `path`, `query`, `body`, `header` |
| `frontier.kind` (internal) | `url`, `action` |
| `frontier.status` (internal) | `pending`, `done`, `skipped`, `queued_approval`, `failed` |
| event `type` | `navigate`, `click`, `fill`, `submit`, `request`, `response`, `dialog`, `state_entered`, `approval_queued`, `approval_started`, `approval_executed`, `login`, `session_lost`, `websocket`, `sse`, `error`, `observation` |

## Tables

### `meta`

| Column | Type | Meaning |
|---|---|---|
| `key` | TEXT PK | Key name |
| `value` | TEXT | Value (numbers and JSON stored as text) |

Public keys: `schema_version` (e.g. `1.0.0`), `crawl_elapsed_s` (total browser-crawl seconds
across invocations). Run id, target, tool version and profile are in `run.json`.

**Internal keys** (bookkeeping; not exported to `appmap.json`):
`approval_failed:<approval id>` (why an approved action could not run),
`approval_inflight:<approval id>` (event id of an approved action let through but not yet
finished), `pass` (current double-back pass, absent = 1), `pass_<n>_baseline` (JSON counts
`{"states","endpoints","routes"}` when pass n began), `pass_<n>_baseline_ts` and
`last_activity_ts` (epoch seconds as text), `probe_active` (JSON progress of
`appmap probe --phase active`).

### `roles`

| Column | Type | Meaning |
|---|---|---|
| `name` | TEXT PK | Role name from the config |
| `login_type` | TEXT | See vocabulary |
| `auth_ok` | INTEGER | 1 = last login verified, 0 = failed, NULL = not attempted |
| `last_login_event` | TEXT | Event id of the last `login` event |

### `states`

One row per unique UI state (role × route pattern × DOM fingerprint).

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT PK | State id (`st-…`) |
| `role` | TEXT | Role that saw it |
| `url` | TEXT | URL of the most recent visit (redacted) |
| `route_pattern` | TEXT | Normalized path pattern (joins `routes.pattern`) |
| `dom_fingerprint` | TEXT | Hash of the DOM skeleton (structure, not text) |
| `title` | TEXT | Document title |
| `first_seen_event` | TEXT | Event id of the first `state_entered` |
| `screenshot` | TEXT | `artifacts/<state id>.png` |
| `dom_snapshot` | TEXT | `artifacts/<state id>.html` (redacted) |
| `visits` | INTEGER | Times the crawler recorded this state |

### `transitions`

Edges between states: performing `action_type` on the element in `locator_set_id` while in
`from_state` led to `to_state`.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `from_state` | TEXT | `states.id`; NULL when the page was loaded directly |
| `to_state` | TEXT | `states.id` |
| `role` | TEXT | Role |
| `action_type` | TEXT | `navigate`, `click`, `submit` |
| `action_label` | TEXT | Link text, button label or form label |
| `locator_set_id` | INTEGER | `locator_sets.id` of the element acted on (for a submit: the submit button, else the last field) |
| `event_id` | TEXT | Event id of the action (`navigate`/`click`/`submit`); NULL for edges recorded from a link to an already visited page |
| `exchange_ids` | TEXT JSON | `[exchanges.id, …]` sent by the role during the action |

### `locator_sets` / `locators`

| Column | Type | Meaning |
|---|---|---|
| `locator_sets.id` | INTEGER PK | |
| `locator_sets.primary_strategy` | TEXT | Strategy of the best (rank 0) locator |
| `locators.id` | INTEGER PK | |
| `locators.locator_set_id` | INTEGER | → `locator_sets.id` |
| `locators.strategy` | TEXT | See vocabulary |
| `locators.value` | TEXT | Selector value for that strategy (Playwright: `get_by_test_id`, `get_by_role(role, name=…)`, `get_by_label`, `get_by_text`, `locator(css)`, `locator("xpath=…")`) |
| `locators.rank` | INTEGER | 0 = most stable; try in ascending order |

### `routes`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `pattern` | TEXT | Route pattern |
| `host` | TEXT | `host[:port]` |
| `kind` | TEXT | `page`, `spa_route` |

UNIQUE(`host`, `pattern`, `kind`). Discovery provenance in `sources` (`item_kind='route'`).

### `endpoints`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `method` | TEXT | HTTP method (upper case) |
| `host` | TEXT | `host[:port]` |
| `path_pattern` | TEXT | Normalized path |
| `kind` | TEXT | See vocabulary |
| `graphql_op` | TEXT | Operation name ('' for non-GraphQL; part of the UNIQUE key) |
| `graphql_type` | TEXT | `query`/`mutation`/`subscription` or NULL |
| `content_types` | TEXT JSON | Request content types seen |

UNIQUE(`method`, `host`, `path_pattern`, `graphql_op`). Provenance in `sources`
(`item_kind='endpoint'`).

### `params`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `endpoint_id` | INTEGER | → `endpoints.id` |
| `location` | TEXT | See vocabulary |
| `name` | TEXT | Parameter name (dotted for nested JSON) |
| `types` | TEXT JSON | Observed types (`str`, `int`, `float`, `bool`, `null`, `list`, `dict`) |
| `examples` | TEXT JSON | Up to 5 example values (redacted where sensitive) |
| `required_evidence` | TEXT | Why it is believed required (e.g. `schema`), else NULL |

### `exchanges`

Every recorded request/response pair.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Increases in recording order |
| `endpoint_id` | INTEGER | → `endpoints.id`; NULL when unattributed (e.g. a guessed path that 404ed) |
| `role` | TEXT | Role whose session sent it; NULL for unauthenticated probes |
| `origin` | TEXT | See vocabulary |
| `event_id` | TEXT | Event id of the `response` event |
| `method` | TEXT | HTTP method |
| `url` | TEXT | Full URL (redacted) |
| `req_headers` | TEXT JSON | Request headers (redacted) |
| `req_body` | TEXT | Request body as text when ≤ 256 KiB, else NULL |
| `req_body_path` | TEXT | `bodies/<id>.req.<ext>` when the body is larger or binary |
| `status` | INTEGER | HTTP status |
| `resp_headers` | TEXT JSON | Response headers (redacted) |
| `resp_body` | TEXT | Response body as text when ≤ 256 KiB, else NULL |
| `resp_body_path` | TEXT | `bodies/<id>.<ext>` when larger or binary |
| `content_type` | TEXT | Response media type without parameters |
| `duration_ms` | REAL | Round-trip time |
| `ts` | TEXT | Epoch seconds (`1792899287.865427`) |

### `responses`

Per endpoint × role summary.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `endpoint_id` | INTEGER | → `endpoints.id` |
| `role` | TEXT | Role |
| `statuses` | TEXT JSON | Sorted distinct statuses |
| `content_types` | TEXT JSON | Response content types |
| `body_shape` | TEXT JSON | JSON key skeleton with leaf type names (first 2xx JSON body; arrays collapse to their first element) |
| `min_size` | INTEGER | Smallest body, bytes |
| `max_size` | INTEGER | Largest body, bytes |
| `sample_exchange_ids` | TEXT JSON | Up to 5 representative `exchanges.id` |

UNIQUE(`endpoint_id`, `role`).

### `forms`

One row each time a form was recorded on a state (the same form on several pages or visits
has several rows; group by label/method/page for an inventory).

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `state_id` | TEXT | → `states.id` where it was found |
| `inferred` | INTEGER | 1 = no `<form>` element (inputs + button grouped by the crawler) |
| `method` | TEXT | Form method (a hidden `_method` override wins); NULL for inferred forms |
| `action_endpoint_id` | INTEGER | → `endpoints.id` the submission hit (first write, else first request) |
| `purpose` | TEXT | Agent-written purpose (from `names.yaml`) |
| `submit_locator_set_id` | INTEGER | → `locator_sets.id` of the submit control |
| `succeeded` | INTEGER | 1 = submitted successfully, 0 = failed, NULL = never submitted (e.g. held for approval) |

The form's label is the `form_label` of the action-path step that targets it
(`approvals.action_path`, or internal `frontier.action_path`), and the flow name.

### `fields`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `form_id` | INTEGER | → `forms.id` |
| `name` | TEXT | Field name (or label when unnamed) |
| `type` | TEXT | Input type (`text`, `email`, `password`, `date`, `select`, `checkbox`, …) |
| `widget` | TEXT | Element kind (`input`, `select`, `textarea`, `div` for custom widgets, …) |
| `constraints` | TEXT JSON | `{"required", "pattern", "min", "max", "maxlength"}` (present keys only) |
| `options` | TEXT JSON | Option values for selects/radios |
| `locator_set_id` | INTEGER | → `locator_sets.id` |
| `values_tried` | TEXT JSON | Values the crawler filled into this field, distinct, oldest first, at most 20 (password fields: `<redacted:password>`; file fields: the file name). NULL until a submission of the form was attempted |
| `validation_messages` | TEXT JSON | Validation messages seen for this field |

### `flows`

Derived by `appmap build` (rebuilt from scratch on every build, so ids are not stable; use
`preconditions.flow_key`). One flow per successful form submission and per executed approved
action. Multi-step forms additionally get one merged flow per chain: B continues A when B's
submit is the very next `submit` event of the same role after A's in `events.jsonl` and
B's restore is just a `goto` of the URL A's submit landed on; the standalone flows are kept.
Flows with the same key are kept once.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT | Default `<role>: <form or action label(s) joined by →> @ <route pattern>`; agent names from `names.yaml` replace it |
| `role` | TEXT | Role |
| `description` | TEXT | Generated numbered step list (`[mutating]` marks writes) until replaced from `names.yaml` |
| `preconditions` | TEXT JSON | See below |

`preconditions` object:

| Key | Meaning |
|---|---|
| `role` | Role the flow must run as |
| `requires` | `[{"endpoint": "METHOD /pattern", "json_path", "producer_exchange_id", "consumer_exchange_id", "consumer_location"}]`: values the flow's requests send that a response outside the flow produced (call that endpoint first and read `json_path`) |
| `flow_key` | Stable key: `role` + `\|`-joined `METHOD /path_pattern` of the flow's action requests (an action without a request contributes `TYPE label@route`). Used to match `names.yaml` across rebuilds |
| `origin` | `ui` (crawled form submission) or `approved` (executed approval) |
| `approval_id` | `approvals.id` for approved flows, else NULL |
| `form_ids` | `forms.id` of the submitted forms |
| `steps` | Same order as `flow_steps` (`goto` steps carry the page load's `method`/`path_pattern`/`status`): `{"ordinal", "type" (goto/click/submit/request), "label", "url", "method", "path_pattern", "status", "mutating", "transition_id", "exchange_id"}`. `mutating` is true for a step that sent a non-GET request or a click the crawler marked as write-issuing; replaying it changes server state |

### `flow_steps`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `flow_id` | INTEGER | → `flows.id` |
| `ordinal` | INTEGER | 1-based order |
| `transition_id` | INTEGER | → `transitions.id` (UI action and its locators), NULL if not recorded |
| `exchange_id` | INTEGER | → `exchanges.id` (the request this step sent; for `goto`, the page load), NULL if none |

A submit that sent several requests has one step per request, all with the same
`transition_id`. Failed attempts (status ≥ 400) and static assets are left out.
To replay a flow, a `goto` step loads its `url` (from `preconditions.steps`, or the
step's page-load exchange); the navigate transition attached to it, if any, is
informational (its `from_state` is one page that links there, not a required step).
Click and submit steps replay through their transition's `locator_set_id`.

### `value_links`

Value provenance, rebuilt by `appmap build`: a leaf of a JSON response, or a path segment /
query value of a redirect's `Location` header (`producer`), later sent by the same role
(`consumer`). Only ints ≥ 2 and strings of 4–256 characters are traced;
booleans, common words, redacted placeholders, the run marker, values the role already sent
before (or in) the producing request, and values present in more than half of the role's
requests are not. Only a value's first producer is linked.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `producer_exchange_id` | INTEGER | → `exchanges.id` whose response contained the value |
| `producer_json_path` | TEXT | JSONPath in that response, e.g. `$.id`, `$.data.items[3].id`, `$["odd key"]`; for a redirect, `$location.path[i]` (i-th path segment, from 0) or `$location.query.<name>` |
| `consumer_exchange_id` | INTEGER | → later `exchanges.id` that sent it |
| `consumer_location` | TEXT | `path` (a whole path segment), `query`, `body`, `header` |
| `value_hash` | TEXT | First 16 hex chars of SHA-256 of the value (the value itself is not stored) |

### `role_access`

Cross-role access matrix (from `appmap probe --phase active`).

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `item_kind` | TEXT | `route` or `endpoint` |
| `item_id` | INTEGER | → `routes.id` / `endpoints.id` |
| `role` | TEXT | Role tested |
| `outcome` | TEXT | See vocabulary |
| `evidence_exchange_id` | INTEGER | → `exchanges.id` proving the outcome |

UNIQUE(`item_kind`, `item_id`, `role`).

### `sources`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `item_kind` | TEXT | `route` or `endpoint` |
| `item_id` | INTEGER | → `routes.id` / `endpoints.id` |
| `source` | TEXT | See vocabulary |

UNIQUE(`item_kind`, `item_id`, `source`).

### `approvals`

Destructive or held actions (mirrored in `approvals.yaml`).

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT PK | `ap-0001`, … |
| `role` | TEXT | Role that would perform it |
| `state_id` | TEXT | → `states.id` where it was found (may be NULL) |
| `category` | TEXT | See vocabulary |
| `label` | TEXT | Control label or URL |
| `reasons` | TEXT JSON | Why it was held |
| `action_path` | TEXT JSON | Steps to perform it: `[{"type": "goto", "url"}, {"type": "click", "label", "locators": [{"strategy","value","rank"}], "mutating"?}, {"type": "submit", "form_label", "form_id", "method", "action", "locators", "has_confirm"}, {"type": "request", "method", "url", "body", "content_type"}]` |
| `predicted_request` | TEXT JSON | `{"method", "url"}` expected to be sent, or NULL |
| `decision` | TEXT | See vocabulary |
| `decided_by` | TEXT | `user`, `rule`, `appmap` |
| `executed_event` | TEXT | Event id of `approval_executed` (its `data.exchange_ids` lists the requests sent) once performed |
| `created_by_run` | INTEGER | 1 when the action was found on the page of a record this crawl just created (a successful submit that landed on that record's URL), else 0. Rule match key `created_by_run: true` in `approvals.yaml`. Added in schema 1.0.x; older databases gain it (default 0) on open |

### `observations`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `kind` | TEXT | See vocabulary |
| `detail` | TEXT | Snippet or status text (redacted) |
| `url` | TEXT | URL |
| `role` | TEXT | Role |
| `event_id` | TEXT | Event id of the `response` |

### `external_links`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `url` | TEXT | Out-of-scope URL (never requested) |
| `found_on` | TEXT | Page URL or redirecting URL where it was seen ('' if unknown) |
| `role` | TEXT | Role |

UNIQUE(`url`, `found_on`).

### Internal tables

`frontier` and `frontier_links` are the crawler's own work queue. They are kept in
`appmap.sqlite` (resumable runs) but are **not** part of the map: they are left out of
`appmap.json` and their shape may change without a schema version bump.

| Column | Type | Meaning |
|---|---|---|
| `frontier.id` | INTEGER PK | |
| `frontier.role` | TEXT | Role |
| `frontier.kind` | TEXT | `url` or `action` |
| `frontier.url` | TEXT | URL to visit ('' never NULL); for actions, the page it starts from |
| `frontier.action_path` | TEXT JSON | Steps for `action` items (same shape as `approvals.action_path`; `'[]'` for urls) |
| `frontier.depth` | INTEGER | Link depth |
| `frontier.priority` | INTEGER | Higher first (`url` 2, `click` 1, `submit` 0) |
| `frontier.status` | TEXT | `pending`, `done`, `skipped`, `queued_approval`, `failed` |
| `frontier.source` | TEXT | Discovery source of the item |
| `frontier_links.id` | INTEGER PK | |
| `frontier_links.role` | TEXT | Role |
| `frontier_links.url` | TEXT | Target URL of an anchor |
| `frontier_links.from_state` | TEXT | State holding the anchor ('' marks the URL visited) |
| `frontier_links.label` | TEXT | Anchor text |
| `frontier_links.locators` | TEXT JSON | Anchor locators |
| `frontier_links.to_state` | TEXT | State the URL landed on, once known |

## `appmap.json`

`{"schema_version": "1.0.0", "run": <run.json>, "<table>": [<row objects>], …}` with one
key per public table above (internal tables and internal `meta` keys left out). JSON columns
are decoded into objects/arrays. `meta` is a list of `{"key", "value"}` rows.

## Query recipes

`appmap query --run <run> <recipe> [args]` runs these (named parameters `:role`, `:endpoint`,
`:method`, `:path`, `:state`). With `sqlite3`, bind them with `.param set :role admin` before
`.read`, or substitute literals.

### `flows`

`appmap query --run R flows [--role R]` — every flow step with its UI action, locator set and
request (`:role` NULL = all roles).

```sql
SELECT f.id AS flow_id, f.name, f.role, fs.ordinal,
       json_extract(f.preconditions, '$.steps[' || (fs.ordinal - 1) || '].type') AS step_type,
       json_extract(f.preconditions, '$.steps[' || (fs.ordinal - 1) || '].mutating') AS mutating,
       t.action_type, t.action_label,
       t.locator_set_id, s.route_pattern AS to_route,
       x.method, coalesce(e.path_pattern, x.url) AS request, x.status,
       fs.transition_id, fs.exchange_id
FROM flows f
JOIN flow_steps fs ON fs.flow_id = f.id
LEFT JOIN transitions t ON t.id = fs.transition_id
LEFT JOIN states s ON s.id = t.to_state
LEFT JOIN exchanges x ON x.id = fs.exchange_id
LEFT JOIN endpoints e ON e.id = x.endpoint_id
WHERE (:role IS NULL OR f.role = :role)
ORDER BY f.id, fs.ordinal;
```

### `forms-posting-to`

`appmap query --run R forms-posting-to --endpoint PATTERN` — forms whose submission hit an
endpoint (`:endpoint` is an exact path pattern or a `LIKE` pattern such as `/api/%`).

```sql
SELECT fo.id AS form_id, s.route_pattern AS page, fo.state_id, fo.method AS form_method,
       e.method || ' ' || e.path_pattern AS action, fo.purpose, fo.succeeded,
       (SELECT group_concat(fi.name, ', ') FROM fields fi WHERE fi.form_id = fo.id) AS fields
FROM forms fo
JOIN endpoints e ON e.id = fo.action_endpoint_id
LEFT JOIN states s ON s.id = fo.state_id
WHERE e.path_pattern = :endpoint OR e.path_pattern LIKE :endpoint
ORDER BY e.path_pattern, fo.id;
```

### `hidden-but-reachable`

`appmap query --run R hidden-but-reachable --role R` — endpoints and routes the role was
`allowed` to access in cross-role replay but never reached through its own UI.

```sql
SELECT 'endpoint' AS item_kind, e.id AS item_id,
       e.method || ' ' || e.path_pattern AS item, ra.outcome, ra.evidence_exchange_id
FROM role_access ra
JOIN endpoints e ON ra.item_kind = 'endpoint' AND e.id = ra.item_id
WHERE ra.role = :role AND ra.outcome = 'allowed'
  AND NOT EXISTS (SELECT 1 FROM exchanges x
                  WHERE x.endpoint_id = e.id AND x.role = :role AND x.origin = 'ui')
UNION ALL
SELECT 'route', r.id, r.pattern, ra.outcome, ra.evidence_exchange_id
FROM role_access ra
JOIN routes r ON ra.item_kind = 'route' AND r.id = ra.item_id
WHERE ra.role = :role AND ra.outcome = 'allowed'
  AND NOT EXISTS (SELECT 1 FROM states st
                  WHERE st.role = :role AND st.route_pattern = r.pattern)
ORDER BY 1, 3;
```

### `endpoint-shape`

`appmap query --run R endpoint-shape --method M --path P` — parameters, then response shape
per role with sample exchange ids.

```sql
SELECT * FROM (
  SELECT 'param' AS part, NULL AS role, p.location || ':' || p.name AS name,
         json_object('types', json(coalesce(p.types, 'null')),
                     'examples', json(coalesce(p.examples, 'null')),
                     'required_evidence', p.required_evidence) AS detail
  FROM endpoints e JOIN params p ON p.endpoint_id = e.id
  WHERE e.method = upper(:method) AND e.path_pattern = :path
  UNION ALL
  SELECT 'response', r.role, NULL,
         json_object('statuses', json(coalesce(r.statuses, 'null')),
                     'content_types', json(coalesce(r.content_types, 'null')),
                     'body_shape', json(coalesce(r.body_shape, 'null')),
                     'min_size', r.min_size, 'max_size', r.max_size,
                     'sample_exchange_ids', json(coalesce(r.sample_exchange_ids, 'null')))
  FROM endpoints e JOIN responses r ON r.endpoint_id = e.id
  WHERE e.method = upper(:method) AND e.path_pattern = :path
)
ORDER BY CASE part WHEN 'param' THEN 0 ELSE 1 END, role, name;
```

### `path-to-state`

`appmap query --run R path-to-state --state ID` — how to reach a state: step 0 loads a URL
(the nearest state loadable by URL — some navigate transition leads to it — or with no
recorded way in), then the shortest chain of click/submit transitions, each with its
locators (best first).

Algorithm (linear in states × depth, never enumerates paths): (1) BFS backwards from the
target over click/submit transitions, `UNION` keeping one row per (state, depth), capped at
depth 12, then `min(depth)` per state = distance to the target; (2) the entry is the
reachable state with the smallest distance that is loadable by URL; (3) reconstruct the path
forward from the entry, at each state taking the lowest-id transition to a state whose
distance is one less, until distance 0. An implementation in a host language should do the
same BFS (one visit per state) and the same tie-break (lowest transition id).

```sql
WITH RECURSIVE
-- 1. BFS backwards from the target over click/submit edges. UNION drops duplicate
--    (state, depth) rows, so the work is bounded by states x max depth (12), not paths.
back(state, depth) AS (
  SELECT :state, 0
  UNION
  SELECT t.from_state, b.depth + 1
  FROM back b JOIN transitions t ON t.to_state = b.state
  WHERE t.action_type != 'navigate' AND t.from_state IS NOT NULL AND b.depth < 12
),
dist(state, d) AS (SELECT state, min(depth) FROM back GROUP BY state),
-- 2. The nearest entry: a state loadable by URL (some navigate edge leads to it) or one
--    with no recorded way in.
best(state) AS (
  SELECT dist.state FROM dist
  WHERE EXISTS (SELECT 1 FROM transitions n WHERE n.to_state = dist.state
                AND n.action_type = 'navigate')
     OR NOT EXISTS (SELECT 1 FROM transitions n WHERE n.to_state = dist.state)
  ORDER BY dist.d, dist.state LIMIT 1
),
-- 3. Walk forward from the entry, each step taking the lowest-id edge to a state one
--    step closer to the target (d decreases by 1), until the target (d = 0).
walk(step, state, tid) AS (
  SELECT 0, best.state, NULL FROM best
  UNION ALL
  SELECT w.step + 1, t.to_state, t.id
  FROM walk w
  JOIN transitions t ON t.id = (
    SELECT t2.id FROM transitions t2 JOIN dist d2 ON d2.state = t2.to_state
    WHERE t2.from_state = w.state AND t2.action_type != 'navigate'
      AND d2.d = (SELECT d FROM dist WHERE state = w.state) - 1
    ORDER BY t2.id LIMIT 1)
  WHERE (SELECT d FROM dist WHERE state = w.state) > 0
)
SELECT w.step, CASE WHEN w.tid IS NULL THEN 'goto' ELSE t.action_type END AS action_type,
       s.url, t.action_label AS label, w.tid AS transition_id, t.locator_set_id,
       CASE WHEN w.tid IS NULL THEN NULL ELSE
         (SELECT json_group_array(json_object('strategy', l.strategy, 'value', l.value,
                                              'rank', l.rank))
          FROM (SELECT * FROM locators WHERE locator_set_id = t.locator_set_id
                ORDER BY rank) l) END AS locators
FROM walk w
LEFT JOIN transitions t ON t.id = w.tid
JOIN states s ON s.id = CASE WHEN w.tid IS NULL THEN w.state ELSE t.from_state END
ORDER BY w.step;
```

### `role-matrix`

`appmap query --run R role-matrix` — the whole access matrix.

```sql
SELECT ra.item_kind,
       CASE ra.item_kind
         WHEN 'route' THEN r.pattern
         ELSE e.method || ' ' || e.path_pattern ||
              CASE WHEN e.graphql_op != '' THEN ' ' || e.graphql_op ELSE '' END
       END AS item,
       ra.role, ra.outcome, ra.evidence_exchange_id
FROM role_access ra
LEFT JOIN routes r ON ra.item_kind = 'route' AND r.id = ra.item_id
LEFT JOIN endpoints e ON ra.item_kind = 'endpoint' AND e.id = ra.item_id
ORDER BY ra.item_kind, item, ra.role;
```

### `gaps`

`appmap query --run R gaps` — coverage gaps (the same lists as `appmap status --gaps`).

```sql
SELECT 'unexercised_endpoint' AS gap, CAST(e.id AS TEXT) AS id,
       e.method || ' ' || e.path_pattern ||
         CASE WHEN e.graphql_op != '' THEN ' ' || e.graphql_op ELSE '' END AS item,
       (SELECT group_concat(s.source, ',') FROM sources s
        WHERE s.item_kind = 'endpoint' AND s.item_id = e.id) AS detail
FROM endpoints e
WHERE NOT EXISTS (SELECT 1 FROM exchanges x WHERE x.endpoint_id = e.id
                  AND x.origin IN ('ui','replay','cross_role','schema','approved'))
  AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.item_kind = 'endpoint' AND s.item_id = e.id
                  AND s.source NOT IN ('bundle','openapi','graphql','robots','sitemap',
                                       'well_known','source_map','wordlist','spa_router'))
UNION ALL
SELECT 'unsuccessful_form', CAST(f.id AS TEXT), f.state_id,
       CASE WHEN f.succeeded IS NULL THEN 'not attempted' ELSE 'failed' END
FROM forms f WHERE f.succeeded IS NOT 1
UNION ALL
SELECT 'unreached_route', CAST(r.id AS TEXT), r.pattern, r.kind
FROM routes r
WHERE NOT EXISTS (SELECT 1 FROM role_access a WHERE a.item_kind = 'route'
                  AND a.item_id = r.id AND a.outcome = 'allowed')
  AND NOT EXISTS (SELECT 1 FROM states s WHERE s.route_pattern = r.pattern)
UNION ALL
SELECT 'unexplored_action', CAST(fr.id AS TEXT), fr.role || ' ' || fr.kind || ' ' ||
       CASE fr.kind WHEN 'url' THEN fr.url ELSE fr.action_path END, fr.status
FROM frontier fr WHERE fr.status IN ('pending','failed')
UNION ALL
SELECT 'approved_not_executed', a.id, a.role || ' ' || coalesce(a.label, ''),
       coalesce((SELECT m.value FROM meta m WHERE m.key = 'approval_failed:' || a.id),
                'approved_pending')
FROM approvals a
WHERE a.decision = 'approve'
  AND (a.executed_event IS NULL
       OR EXISTS (SELECT 1 FROM meta m WHERE m.key = 'approval_failed:' || a.id))
UNION ALL
SELECT CASE a.decision WHEN 'pending' THEN 'pending_approval' ELSE 'denied_approval' END,
       a.id, a.role || ' ' || coalesce(a.category, '') || ' ' || coalesce(a.label, ''),
       a.reasons
FROM approvals a WHERE a.decision IN ('pending','deny')
ORDER BY 1, 2;
```

## More examples

`flow-form-fields` — every field of the forms a flow submits, with locators and the body the
flow actually sent (`:flow` = `flows.id`):

```sql
SELECT f.id AS flow_id, f.name, fo.id AS form_id, fi.name AS field, fi.type, fi.widget,
       fi.constraints, fi.options,
       (SELECT json_group_array(json_object('strategy', l.strategy, 'value', l.value))
        FROM (SELECT * FROM locators WHERE locator_set_id = fi.locator_set_id
              ORDER BY rank) l) AS field_locators,
       (SELECT x.req_body FROM flow_steps fs
        JOIN transitions t ON t.id = fs.transition_id AND t.action_type = 'submit'
        JOIN exchanges x ON x.id = fs.exchange_id
        WHERE fs.flow_id = f.id AND t.from_state = fo.state_id
        ORDER BY fs.ordinal LIMIT 1) AS submitted_body
FROM flows f
JOIN json_each(f.preconditions, '$.form_ids') j
JOIN forms fo ON fo.id = j.value
JOIN fields fi ON fi.form_id = fo.id
WHERE f.id = :flow
ORDER BY fo.id, fi.id;
```

```sql
-- request/response pairs of a flow, in order
SELECT fs.ordinal, x.method, x.url, x.status, x.req_body, x.resp_body
FROM flow_steps fs JOIN exchanges x ON x.id = fs.exchange_id
WHERE fs.flow_id = 1 ORDER BY fs.ordinal;

-- where a value used by a request came from
SELECT v.consumer_location, v.producer_json_path, p.method, p.url
FROM value_links v JOIN exchanges p ON p.id = v.producer_exchange_id
WHERE v.consumer_exchange_id = 42;

-- endpoints only some roles can reach
SELECT e.method, e.path_pattern, group_concat(ra.role || '=' || ra.outcome, ', ')
FROM role_access ra JOIN endpoints e ON ra.item_kind = 'endpoint' AND e.id = ra.item_id
GROUP BY e.id HAVING count(DISTINCT ra.outcome) > 1;

-- records created by this run (as in APP-MAP.md): every exchange in the run directory was
-- sent by this run, so all successful writes except login traffic are candidates
SELECT id, role, origin, method, url, status FROM exchanges
WHERE upper(method) NOT IN ('GET','HEAD','OPTIONS') AND status BETWEEN 200 AND 399
  AND origin != 'login'
ORDER BY id;
-- APP-MAP.md also drops login/logout endpoints and annotates each row with the run marker
-- (or a run-specific truncation of it) found in its query string or body; to search for
-- the full marker yourself, substitute it from run.json and escape '_'/'%':
--   AND (req_body LIKE '%appmap-20261024-120000%' ESCAPE '\' OR url LIKE '%appmap-20261024-120000%' ESCAPE '\')
-- (bodies over 256 KiB are in req_body_path files and not covered by LIKE)
```
