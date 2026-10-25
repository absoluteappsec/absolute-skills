---
name: build-app-map
description: Use during an appmap run, after crawling, probing and approvals have converged, to build the final application map - run appmap build to derive flows and value links and write appmap.json, APP-MAP.md and SCHEMA.md, then name and describe each flow and give each form a purpose in names.yaml, rebuild, review APP-MAP.md, and report results with the data-handling notice. Triggers on "build the app map", "write APP-MAP.md", "name the flows", "finish the appmap run", "summarize what the crawl found".
---

## Purpose

`appmap build` turns the crawl database into the deliverables: flows (ordered UI actions and
requests, with value provenance and preconditions), `appmap.json`, `APP-MAP.md`, a copy of
`SCHEMA.md`, and a `names.yaml` stub. The mechanical parts are generated; the **narrative** —
flow names, flow descriptions and form purposes — is yours. It goes into `names.yaml`, which every
build applies, and from there into `APP-MAP.md`, `appmap.sqlite` and `appmap.json`.

Do not hand-edit `APP-MAP.md`: every build rewrites it.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`.

---

## Step 1 — First Build

`appmap build --run <run>`

Status `complete`; `counts`: `flows`, `flow_steps`, `value_links`, `preconditions`,
`names_applied`, `forms`, `endpoints`, `routes`, `states`; `pending_approvals` still undecided.
`next` says to edit `<run>/names.yaml` and rebuild. Building is idempotent and can be repeated at
any time.

---

## Step 2 — Understand the Flows and Forms

For each flow, look at what it actually does before naming it:

- `appmap query --run <run> --format table flows` (or `--role <role>`) — every step: UI action,
  label, locator set, request, status.
- `appmap query --run <run> flow-form-fields --flow <flow id>` — the fields it submits and the
  body it sent.
- The flow's preconditions (values it needs from elsewhere):
  `appmap query --run <run> sql "SELECT id, name, role, json_extract(preconditions, '$.requires') AS requires FROM flows"`.
- Forms: `APP-MAP.md` → "Forms" (page, method, action endpoint, fields, result).

---

## Step 3 — Write names.yaml

`<run>/names.yaml` (generated; a rebuild only appends new entries, never changes yours):

```yaml
flows:
- id: 3
  key: "admin|POST /projects"          # stable match key — do not edit
  name: "admin: New project → Create @ /projects/new"
  description: ""
forms:
- id: 12
  label: "New project"
  purpose: ""
```

Fill in:

- **Flow `name`** — a short business action in the user's words: "Create project", "Invite team
  member", "Change notification settings". Not the URL.
- **Flow `description`** — one or two sentences: who does it, what changes, what it depends on
  (e.g. "Admin creates a project from the dashboard; the returned project id is used by the
  member-invite flow. Requires an existing organization.").
- **Form `purpose`** — what the form is for ("Create a project", "Search orders by status"). One
  entry covers every copy of the same form (same label, method and page).

Base names on evidence (steps, requests, screenshots in `<run>/artifacts/`), not guesses. Leave a
value empty if you cannot tell — empty values are ignored. Keep `id`/`key` as generated.

---

## Step 4 — Rebuild and Review

`appmap build --run <run> --names <run>/names.yaml`

(`names.yaml` in the run directory is applied by every build anyway; `--names` also accepts a
names file kept elsewhere.) `counts.names_applied` counts values that changed in this build (non-zero right after you
edited names; 0 on a repeat build with the same names). `next` is `null`. Then read `APP-MAP.md`
top to bottom:

- run summary (roles, auth status, pass number), route tree, endpoint inventory, role access
  matrix, key flows, forms, GraphQL operations;
- **Unexercised surface** (pending/denied approvals), **Coverage gaps**, **Observations** (5xx,
  stack traces, debug pages, CAPTCHA, rate limiting);
- **Records created or changed by this run** — the cleanup/restore list for the operator:
  successful writes plus every executed approved action, whatever its method (a GET delete).

If something looks wrong (a role with no states, an empty matrix, a flow you could not name), go
back to the relevant phase skill rather than papering over it. Structure of the document:
`references/app-map-template.md`.

---

## Step 5 — Report to the Operator

Give a short report: target, profile, roles and their auth status, counts (states, routes,
endpoints, forms, flows), passes run and why the double back stopped, notable access-control
results (e.g. `hidden-but-reachable` items), unexercised surface and gaps with reasons, records
created (for cleanup), and the run directory path.

If `run.json` has `contains_production_data: true`, include the handling notice: the directory
holds real customer data; store it encrypted, share only with people authorized for the
engagement, delete it when the engagement ends; saved sessions in
`~/.cache/absolute-appmap/sessions/<run_id>/` should be deleted too.

---

## Completion Criteria

- [ ] `appmap build` completed; `names.yaml` generated.
- [ ] Every flow has an evidence-based name and description; every form a purpose (or a
      deliberate blank where unknowable).
- [ ] Rebuilt after editing `names.yaml` (`names_applied` > 0 on that build); `APP-MAP.md`
      reviewed end to end.
- [ ] Report delivered, including created records and, for production, the handling notice.

Return to `map-running-application` — the run is complete.

---

## Reference Files

- `references/app-map-template.md` — the sections of `APP-MAP.md`, where each comes from, and
  what to check in each.
- `../query-app-map/SKILL.md` — querying the finished map.
