# Example queries

Run any of these with `appmap query --run <run> sql "<statement>"` (add `--format table` for
aligned output) or with `sqlite3 -readonly <run>/appmap.sqlite` (always read-only). Replace literals such as `'admin'`,
`42` or `'st-…'` with your values. Recipes (`appmap query --run <run> <recipe>`) cover the most
common questions; their SQL is in `schema.md` → "Query recipes".

## Orientation

```sql
-- schema version and crawl time
SELECT key, value FROM meta WHERE key IN ('schema_version', 'crawl_elapsed_s');

-- roles and whether they authenticated
SELECT name, login_type, auth_ok FROM roles ORDER BY name;

-- size of the map
SELECT (SELECT count(*) FROM states) AS states, (SELECT count(*) FROM routes) AS routes,
       (SELECT count(*) FROM endpoints WHERE kind != 'static') AS endpoints,
       (SELECT count(*) FROM forms) AS forms, (SELECT count(*) FROM flows) AS flows;
```

## Flows for a role

```sql
-- all flows of role 'user', with how many steps write
SELECT f.id, f.name, f.description,
       (SELECT count(*) FROM json_each(f.preconditions, '$.steps') s
        WHERE json_extract(s.value, '$.mutating') = 1) AS mutating_steps
FROM flows f WHERE f.role = 'user' ORDER BY f.id;
```

Recipe: `appmap query --run <run> flows --role user`.

## Every form posting to an endpoint

Recipe: `appmap query --run <run> forms-posting-to --endpoint '/api/projects%'`.

```sql
-- forms grouped by the endpoint they submit to
SELECT e.method || ' ' || e.path_pattern AS endpoint, count(*) AS forms,
       group_concat(DISTINCT coalesce(fo.purpose, '')) AS purposes
FROM forms fo JOIN endpoints e ON e.id = fo.action_endpoint_id
GROUP BY e.id ORDER BY endpoint;
```

## Reachable by `user`, only linked for `admin`

```sql
-- endpoints the user role is allowed to call but only admin used from its UI
SELECT e.method, e.path_pattern
FROM endpoints e
JOIN role_access ra ON ra.item_kind = 'endpoint' AND ra.item_id = e.id
WHERE ra.role = 'user' AND ra.outcome = 'allowed'
  AND EXISTS (SELECT 1 FROM exchanges x WHERE x.endpoint_id = e.id AND x.role = 'admin' AND x.origin = 'ui')
  AND NOT EXISTS (SELECT 1 FROM exchanges x WHERE x.endpoint_id = e.id AND x.role = 'user' AND x.origin = 'ui')
ORDER BY e.path_pattern;
```

General form: `appmap query --run <run> hidden-but-reachable --role user`.

## Request/response shape of an endpoint

Recipe: `appmap query --run <run> endpoint-shape --method POST --path /api/projects`.

```sql
-- one representative successful exchange per role, with bodies
SELECT x.role, x.status, x.req_body, x.resp_body
FROM exchanges x JOIN endpoints e ON e.id = x.endpoint_id
WHERE e.method = 'POST' AND e.path_pattern = '/api/projects' AND x.status BETWEEN 200 AND 299
GROUP BY x.role;
```

## Action path to reach a state

Recipe: `appmap query --run <run> path-to-state --state st-0123456789ab`.

```sql
-- states of a route pattern, to pick a target state id
SELECT id, role, url, title, screenshot FROM states WHERE route_pattern = '/projects/{id}';

-- ranked locators of one locator set (use rank 0 first)
SELECT strategy, value, rank FROM locators WHERE locator_set_id = 57 ORDER BY rank;
```

## Chaining values between requests

```sql
-- where each value a request sent came from
SELECT v.consumer_location, v.producer_json_path, p.method, p.url AS producer_url
FROM value_links v JOIN exchanges p ON p.id = v.producer_exchange_id
WHERE v.consumer_exchange_id = 42;

-- flows that need data produced outside the flow
SELECT f.id, f.name, r.value AS requirement
FROM flows f, json_each(f.preconditions, '$.requires') r;
```

## GraphQL

```sql
SELECT graphql_type, graphql_op, path_pattern FROM endpoints
WHERE kind = 'graphql_op' ORDER BY graphql_type, graphql_op;
```

## Access control

```sql
-- items where roles get different outcomes
SELECT e.method, e.path_pattern, group_concat(ra.role || '=' || ra.outcome, ', ') AS outcomes
FROM role_access ra JOIN endpoints e ON ra.item_kind = 'endpoint' AND e.id = ra.item_id
GROUP BY e.id HAVING count(DISTINCT ra.outcome) > 1;

-- anonymous access to anything
SELECT ra.item_kind, ra.item_id, ra.outcome FROM role_access ra
WHERE ra.role = 'anonymous' AND ra.outcome = 'allowed';
```

## Coverage and provenance

```sql
-- endpoints known only from bundles/schemas and never exercised (also: recipe gaps)
SELECT e.method, e.path_pattern, group_concat(s.source) AS sources
FROM endpoints e JOIN sources s ON s.item_kind = 'endpoint' AND s.item_id = e.id
WHERE NOT EXISTS (SELECT 1 FROM exchanges x WHERE x.endpoint_id = e.id)
GROUP BY e.id;

-- forms that never submitted successfully, with validation messages
SELECT fo.id, fo.succeeded, fi.name, fi.validation_messages
FROM forms fo LEFT JOIN fields fi ON fi.form_id = fo.id
WHERE fo.succeeded IS NOT 1 ORDER BY fo.id;

-- held actions and their decisions
SELECT id, role, category, label, decision, executed_event FROM approvals ORDER BY id;
```

## Observations and evidence

```sql
-- server errors and stack traces seen
SELECT kind, role, url, substr(detail, 1, 120) AS detail FROM observations ORDER BY kind;

-- the event behind an exchange (then grep events.jsonl for that id)
SELECT id, event_id, method, url, status FROM exchanges WHERE id = 42;
```

Events: each line of `events.jsonl` is `{"id", "ts", "type", "role", "caused_by", "data"}`;
follow `caused_by` backwards from a `response` event to the click or submit that caused it.
