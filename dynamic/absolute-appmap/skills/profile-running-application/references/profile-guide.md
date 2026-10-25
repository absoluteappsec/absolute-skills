# Profile guide: judging and writing the narrative

How to turn `profile.json` into the purpose, feature names, descriptions and "how it works"
text in `profile-names.yaml` — with evidence, at the right confidence, and nothing you may not
write.

## Confidence

Three levels, used by `profile.json` and by you:

| Level | Means | Examples |
|---|---|---|
| `confirmed` | An explicit name/version from an authoritative source, or two independent detectors agreeing | `Server: Werkzeug/3.1.9 Python/3.12.13` header; a `/*! jQuery v3.6.0` banner; a Django debug page's version row; OpenAPI `"openapi": "3.0.3"`; `<meta name="generator">`; a `csrftoken` cookie *and* a `csrfmiddlewaretoken` form field |
| `likely` | One indirect marker, or an inference | A `sessionid` cookie name alone; a DOM marker; a script URL; Python inferred from Django |
| `guessed` | Your inference without a detector | "Probably a ticketing tool for support staff" from page titles |

Rules:

- The command assigns `confirmed`/`likely` to technologies; it never emits `guessed`. Only
  your prose can be `guessed`, and it must say so ("appears to", "probably").
- You may **downgrade** (write about a `confirmed` claim as `likely` when its evidence is
  weaker than the label — e.g. both detectors read the same generic cookie) and you must say
  why in your report. You may **never upgrade** without new evidence, and you never add a
  technology no detector found — mention it as an open question in the report instead.
- Versions: report every version listed; conflicting versions (different environments, or a
  CDN copy and a bundled copy) are all kept — do not pick one.
- A technology without a version is fine; no body means no version, and nothing is re-fetched.

## Citing evidence in prose

Every factual sentence in `purpose`, `description` and `how_it_works` ends with one or more refs
in square brackets: `[<kind> <id>]`, where `kind` is one of `exchange`, `state`, `flow`, `form`,
`endpoint`, `route`, `file` (the same kinds as `profile.json` refs).

```text
Members create a project from /projects/new; the form posts title and description
[form 12] [exchange 87], and the server redirects to /projects/{id} [exchange 88].
```

- Several runs: prefix the run id — `[20261025-113007 exchange 42]`. One run: no prefix.
- Prefer the most specific ref: an exchange id over an endpoint, a flow over a route.
- Snippets: quote at most a short phrase (under 200 characters) and only from stored data;
  never a password, token, cookie value or personal data beyond what the task needs.
- An inference cites what it is inferred from and is marked as such: "Appears to be an
  internal tool for support staff (guessed) — every role is staff, titles say 'Helpdesk'
  [state st-1a2b3c]".

## Inferring the purpose

Look, in this order, at:

1. Page titles, headings and navigation labels of the first states per role
   (`layout.entry_points`, `APP-MAP.md` route tree, `artifacts/<state id>.png`).
2. The feature set and entity names (`features[].resource`, `data_model[].name`): projects,
   invoices, tickets, appointments …
3. Roles and what each reaches (`accounts.roles`, `coverage.features_x_roles`): who is the
   user, who is the administrator, what is public.
4. API schema `info` (title, description) when `data_model[].source` is `openapi`.

Write two to four sentences: what it is, who uses it, the core thing they do. No marketing
language, no guesses stated as facts.

## Naming and describing features

The command clusters features by primary resource (path segment) plus the flows that touch it;
`name_suggestion` is just the resource in title case.

- **Name**: a business capability in the user's words — "Manage projects", "Invite team
  members", "View admin statistics". Not a URL, not a table name.
- **Description**: one or two sentences — who does it, what changes, what it depends on.
- **How it works**: the entry point (a route from `features[].routes` or
  `layout.entry_points`), the pages and forms in order, the API calls in order (from the flow
  steps: `appmap query --run <run> flows`), values passed between steps (`value_links`,
  `data_setup.preconditions`), and which roles can do it (`coverage.features_x_roles`). Cite
  each step.
- A cluster that is really two features, or two clusters that are one: describe what you see in
  each entry and say so in your report — keys are stable, so do not invent new ones.
- A cluster you cannot explain: keep the suggestion as the name, leave the rest empty, and list
  it under "could not determine".

## Data setup and accounts

Point testers at what they need before a flow works: `data_setup.preconditions` (values from
earlier responses), `created_records` (marker records — also the cleanup list),
`seed_observations` (pre-existing ids), `fixtures_used`; `accounts.roles` (login type, whether
authentication worked, reach). Use this in "how it works" where a feature depends on it.

## What never to write

- Judgments of any kind: "outdated", "vulnerable", "insecure", "deprecated", "end-of-life",
  "risky", CVE ids, severity, recommendations to upgrade. The profile is descriptive; analysis
  belongs to later skills.
- Credentials and secrets: passwords, session cookie values, bearer tokens/JWTs, CSRF token
  values, API keys, env-var values, anything from `appmap.secrets.env`. The run directory is
  redacted (`<redacted:KIND>`); never try to recover or reconstruct a redacted value.
- Unmasked observed identities from a production run unless the operator asked for
  `--include-observed-accounts`.
- Anything from outside the run directory (source code, docs found online, your memory of the
  product) presented as observed fact.
