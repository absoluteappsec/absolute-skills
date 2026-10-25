# SPA techniques: how the crawler handles single-page apps

The SPA layer runs inside `appmap crawl`. This note explains what it does, how to tell whether it
engaged, and which config knobs to adjust. You never script the browser yourself.

## Detection

With `spa: auto` (default) each loaded page is checked for a framework and a client router:

| Framework | Detected by | Runtime route extraction |
|---|---|---|
| Next.js | `__NEXT_DATA__` / `next` | yes, plus `/_next/static/<buildId>/_buildManifest.js` |
| Nuxt | `__NUXT__` / `$nuxt` / `#__nuxt` | yes (router) |
| SvelteKit | `__sveltekit*` globals | yes (manifest) |
| Vue | `__vue_app__`, `Vue`, `[data-v-app]` | yes when Vue Router is present |
| Angular | `[ng-version]` | no — routes come from bundle mining |
| React | React DevTools hook / fiber keys | no — React Router has no runtime hook; routes come from bundle mining |

`spa: on` forces the layer when the router is not detected; `spa: off` disables it. Route
patterns found anywhere (runtime, bundles, source maps) are stored in `routes` with
`kind = 'spa_route'` and navigated directly for every role, filling parameters (`:id`,
`{id}`) with ids observed in that role's responses.

That is why `probe --phase passive` (bundle mining) runs **before** the crawl: for React and
Angular apps, bundles are the main source of client routes.

## What changes for SPAs

- **Clickables beyond anchors:** elements with click listeners (found through the browser's
  debugging protocol), ARIA roles `button`, `link`, `tab`, `menuitem`, `option`,
  `cursor: pointer` and `[onclick]`. Menus, tabs, accordions, dropdowns and hover-revealed
  controls are expanded to surface hidden actions.
- **State graph, not URL list:** a state is the role + route pattern + a DOM-skeleton fingerprint
  (tags, roles, landmarks; text, ids, timestamps and counts stripped). A client-side route change
  always yields a transition.
- **Restoration by replay:** to explore from an earlier state the crawler reloads a known entry
  URL and replays the recorded clicks, rather than trusting Back. Clicks that issued writes are
  marked `mutating` and not replayed for restoration.
- **Settledness:** an action is done when the network is idle and the DOM stopped changing for
  `limits.quiet_window_ms` (default 500).
- **Sampling:** at most `limits.max_states_per_route_pattern` (default 5) states per route
  pattern — this bounds infinite scroll, pagination, virtualized lists and near-duplicate
  detail pages.
- **API surface:** GraphQL is recorded per operation name and type; WebSocket and SSE messages
  are recorded; bearer tokens kept in web storage are captured per role (and redacted in output).
- **Service workers are blocked** so cached responses cannot hide traffic.
- **Dialogs:** `alert`/`prompt` are recorded and accepted; `confirm()` is dismissed and the
  action is held for approval.

## Diagnosing a thin SPA crawl

| Symptom (query) | Likely cause | Adjust |
|---|---|---|
| No `spa_route` rows and few states, but the app is an SPA | Router not detected | `spa: on`; make sure `discovery.bundle_mining: true` and rerun `probe --phase passive` |
| Many states missing content / actions not found after restore | App still rendering when fingerprinted | Raise `limits.quiet_window_ms` (e.g. 1000–2000) |
| Lists only partly sampled | Sampling cap | Raise `max_states_per_route_pattern` only if the operator wants more samples |
| States stop at a fixed depth | `max_depth` reached | Raise `limits.max_depth` |
| A role sees only the login page | Session lost / token not captured | Rerun `authenticate-roles` for that role |

Config edits take effect on the next command. Items already crawled are not revisited, so
settings that change how states are recognised are best set before the crawl starts.

Queries:

```
appmap query --run <run> --format table sql "SELECT kind, count(*) FROM routes GROUP BY kind"
appmap query --run <run> --format table sql "SELECT role, route_pattern, count(*) AS states FROM states GROUP BY role, route_pattern ORDER BY states DESC"
appmap query --run <run> sql "SELECT r.pattern, group_concat(s.source) FROM routes r JOIN sources s ON s.item_kind = 'route' AND s.item_id = r.id WHERE r.kind = 'spa_route' GROUP BY r.id"
```
