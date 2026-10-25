# APP-MAP.md: structure and review checklist

`APP-MAP.md` is rendered by `appmap build` from `appmap.sqlite` every time (never edit it by
hand). Long sections are truncated with a pointer to the SQL that lists everything. Your
narrative enters through `names.yaml` (flow names/descriptions, form purposes).

| Section | Source | What to check |
|---|---|---|
| Handling notice (production only) | `run.json` `contains_production_data` | Present for production runs; repeat it in your report |
| Run summary | `run.json`, `roles`, `meta` | Target(s), run id, profile, every role with login type and auth status, crawl time and pass number, counts |
| Route tree | `routes` + `sources` + `states` | Nested by path; SPA routes marked; sources per route; "not reached" routes |
| Endpoint inventory | `endpoints`, `responses`, `sources` | Grouped by resource prefix; method, path pattern, kind, statuses, roles seen, sources; static assets counted separately |
| Role access matrix | `role_access` | Outcomes per item × role; empty means `probe --phase active` has not run |
| Key flows | `flows`, `flow_steps`, `preconditions` | Your name and description; numbered steps (goto / click / submit / request) with method, path, status; `[mutating]` steps; preconditions (values produced outside the flow) |
| Forms | `forms`, `fields` | Page, method, action endpoint, your purpose, result (succeeded / failed / not submitted), fields |
| GraphQL operations | `endpoints` (`kind = 'graphql_op'`) | Type, operation name, endpoint, sources, exchange count |
| Unexercised surface | `approvals` (pending, deny) | Held actions never performed, with reasons |
| Coverage gaps | same lists as `appmap status --gaps` | Endpoints only from discovery sources; forms never submitted successfully; routes reached by no role; unexplored or failed actions |
| Observations | `observations`, `external_links` | 5xx, stack traces, debug pages, CAPTCHA, rate limiting; out-of-scope link count |
| Records created by this run | `exchanges` (successful non-GET, non-login) | Cleanup candidates; marker matches listed first |
| Where to dig deeper | — | Pointers to `appmap.sqlite`, `SCHEMA.md`, `events.jsonl`, `traffic/`, `appmap query` recipes |

## Naming guidance

| Generated name | Better name | Description example |
|---|---|---|
| `admin: New project → Create @ /projects/new` | Create project | Admin creates a project from the projects page. Returns the project id used by "Invite member". |
| `user: Profile @ /account` | Update profile | User changes display name and timezone. No preconditions. |
| `admin: Delete project @ /projects/{id}` (origin `approved`) | Delete project (approved) | Executed after operator approval on a project this run created. |

Form purposes: a verb phrase naming the business goal ("Create a project", "Filter orders by
status", "Upload an avatar"). For search/filter forms say so — it tells the test-generation
consumer the form does not change state.

## Final review questions

1. Does every role have states and an auth status of "auth ok"?
2. Is the role access matrix populated, and do `hidden-but-reachable` results make sense?
3. Are the remaining gaps explained (approval denied, operator-only data, limits) in your report?
4. Are the records created by this run listed for the operator's cleanup?
5. For production: is the handling notice present and repeated to the operator?
