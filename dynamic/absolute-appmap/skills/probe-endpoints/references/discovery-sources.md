# Discovery sources

Every discovered route or endpoint records which source(s) found it in the `sources` table
(`item_kind` `route`/`endpoint`, `source`), and every request records its `exchanges.origin`.
That lets the map tell "linked in the UI" from "only in a bundle" from "only in a schema".
All requests — browser or direct — pass the same scope, throttle, circuit-breaker, redaction and
approval gates.

## Passive phase (`appmap probe --run <run> --phase passive`)

| Source (`sources.source`) | What is requested | Config switch |
|---|---|---|
| `robots` | `/robots.txt` of each base URL; `Allow`/`Disallow` paths become routes, `Sitemap:` lines are followed | `passive_sources` |
| `sitemap` | `/sitemap.xml` and sitemaps named in robots.txt, following sitemap indexes (depth ≤ 3) | `passive_sources` |
| `well_known` | `/.well-known/security.txt`, `/.well-known/openid-configuration`, `/manifest.json` | `passive_sources` |
| `openapi` | `/openapi.json`, `/swagger.json`, `/v3/api-docs`, `/api-docs`, `/swagger/v1/swagger.json`, `/api/openapi.json`, plus any openapi/swagger URL already seen; every operation becomes an endpoint with schema parameters (`params.required_evidence = 'schema'`) | `schemas` |
| `graphql` | Introspection query to `/graphql`, `/api/graphql` and GraphQL endpoints already seen; every query/mutation/subscription becomes a `graphql_op` endpoint | `schemas` |
| `bundle` | JavaScript bundles of the start pages: API path strings, `fetch`/`axios`/XHR call sites, base URLs, route tables, GraphQL documents | `bundle_mining` |
| `source_map` | Exposed source maps of those bundles (same mining over original sources) | `bundle_mining` |

Requests of this phase have `exchanges.origin` `passive` (or `bundle` for URLs mined from
bundles). Discovered in-scope page URLs are added to the crawl frontier (`counts.queued`);
out-of-scope URLs go to `external_links` and are never requested.

## Browser crawl (`appmap crawl`)

`ui` — anchors, clickables, forms and the requests the app itself makes; `spa_router` — client
routes read from the framework runtime. Origin `ui`.

## Active phase (`appmap probe --run <run> --phase active`)

| Step | What is sent | Origin / source |
|---|---|---|
| API replay | Each captured API call (from `ui`, `passive`, `bundle` traffic) replayed per role with variations: `OPTIONS` and `HEAD`, each optional query parameter omitted once, pagination (`page=2`, `offset=10`, `limit=5`), and other ids observed in responses substituted into a path parameter | origin `replay`, source `replay` |
| Schema/bundle-only endpoints | Endpoints never seen in traffic, exercised with schema-derived or inferred parameters | origin `schema` |
| Forced browsing | Built-in wordlist plus `discovery.wordlists`; only when `forced_browsing: true` (refused in production) | source `wordlist` |
| Cross-role replay | Every route and endpoint discovered by any role, requested as every other role | origin `cross_role`; fills `role_access` |

State-changing replays obey the approval rules: destructive requests (any DELETE, the
destructive keyword categories) are held; in production every write is held. Cross-role
POST/PUT/PATCH replays are sent without approval because they measure access control. Requests
whose captured form contained redacted credentials are skipped. Endpoints tied to an approval or
to logout are not replayed (`not_tested`).

## Access-matrix outcomes (`role_access.outcome`)

| Outcome | Meaning |
|---|---|
| `allowed` | 2xx with the same JSON shape as the discovering role's response |
| `redirect_login` | 401, or the request landed on the login page |
| `forbidden` | 403 |
| `not_found` | 404 / 410 |
| `different_content` | 3xx elsewhere, 400/422 (authorization passed, input rejected), or 2xx with a different JSON shape |
| `error` | 405, 5xx or network failure |
| `not_tested` | Held for approval, logout, or tied to an approval |

`allowed` for a role that never reached the item in its own UI is reported by
`appmap query --run <run> hidden-but-reachable --role <role>` — "hidden in the UI" versus
"actually blocked".

## Reading coverage

- Endpoints only from discovery sources and never exercised: `appmap status --run <run> --gaps`
  → `unexercised_endpoints` (each with its `sources`).
- Provenance of one endpoint:
  `appmap query --run <run> sql "SELECT e.method, e.path_pattern, group_concat(s.source) AS sources FROM endpoints e JOIN sources s ON s.item_kind = 'endpoint' AND s.item_id = e.id GROUP BY e.id ORDER BY e.path_pattern"`
