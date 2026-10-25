---
name: profile-running-application
description: Use when someone needs to come up to speed on a web application that an absolute-appmap run has already mapped - its purpose, main features, technology stack with versions, layout, how each feature works, data model, data setup, test accounts, environments and coverage - e.g. before planning full test coverage or generating tests. Profiles the running application from its appmap run directory (one per environment), not from source code (for a source-code portrait use absolute-codereview's profile-application). Runs appmap profile, writes the purpose and feature names, descriptions and how-it-works prose with evidence refs into profile-names.yaml, and re-runs to render APP-PROFILE.md. Triggers on "profile the app", "profile the running application", "write APP-PROFILE.md", "what is this app built with", "brief me on the mapped application", "what does this app do".
---

## Purpose

`appmap profile` turns one or more finished run directories into an **application profile**: a
briefing for a QA tester, developer or test-generation agent who has never seen the
application. It leads with what the application is for and what it does, then how it is built
(language, frameworks, libraries, servers, auth mechanism, API styles — with versions when
observable), how it is laid out, how each feature works, its data model, the data and accounts
a tester needs, which environments the evidence comes from, and what the map did *not* see.

Two layers, like `build`'s `names.yaml`:

- The **command** extracts everything mechanical into `profile.json` (structured, versioned,
  every claim with evidence refs and a confidence) and renders `APP-PROFILE.md`.
- **You** write the narrative — purpose, feature names, descriptions, "how it works" — into
  `profile-names.yaml`. Every re-run applies it.

Ground rules:

- **Dynamic only.** Everything comes from the run directory. No source code, no new requests
  to the target, no browsing. The command never contacts the target and never writes to a
  run's `appmap.sqlite`.
- **Descriptive only.** Say what the application *is*, never whether something is outdated,
  vulnerable, insecure or risky; no CVE lookups. Later skills analyze.
- **Evidence or silence.** Every factual sentence you write cites evidence refs; a thing you
  cannot support stays unsaid and goes into the report as "could not determine".
- **Never** copy passwords, tokens, session values, env-var values or `appmap.secrets.env`
  content into any file or message.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`. Do not hand-edit
`APP-PROFILE.md` or `profile.json`: every run rewrites them.

---

## Step 1 — Check the Inputs

You need one run directory per environment of **the same application** (e.g. staging and
production). For each:

1. `run.json` → `status` should be `complete` (the map was built); note `profile` and
   `contains_production_data`.
2. `appmap status --run <run>` → `pending_frontier` and `approvals` show whether the crawl
   finished. (If the directory belongs to someone else and must stay untouched, read
   `run.json` and `APP-MAP.md` instead; `appmap profile` itself only reads the run.)

If a run is not built or not finished, say what is missing (no `appmap build` → no flows, value
links or feature clustering; failed roles → no access data for them). The profile still works on
such runs — it reports `partial` and lists the gaps — so continue unless the operator wants to
finish the run first (`map-running-application`).

---

## Step 2 — Run the Profile

`appmap profile --run <run>`

Several environments: repeat `--run` (`appmap profile --run <staging run> --run <prod run>`).
Output goes to the first run directory; pass `--out <dir>` to write elsewhere (required when that
directory is not writable, or should stay as it is).

| `status` | Meaning |
|---|---|
| `profiled` | Every section was derived |
| `partial` | Written, but something could not be determined (unbuilt run, failed role, unfinished crawl, unreadable section) — listed in `profile.json` `could_not_determine` and in APP-PROFILE.md "Could not determine" |
| `error` | Nothing written (no `appmap.sqlite`, unwritable output directory — `errors` says why) |

`counts`: `technologies`, `features`, `entities`, `environments`. `errors` carries warnings
(e.g. an unparseable `profile-names.yaml`, left untouched and copied to `.bak`). `next` asks you
to fill `profile-names.yaml` and re-run while the purpose or any feature name is missing.

Outputs: `profile.json`, `APP-PROFILE.md`, `profile-names.yaml`.

**Production inputs:** if any run has `contains_production_data: true`, observed usernames and
emails are masked (`observed_accounts_masked: true`) and APP-PROFILE.md carries the handling
notice. Add `--include-observed-accounts` only when the operator asks for unmasked identities.
Accounts the run created (they contain the run marker) are always shown.

---

## Step 3 — Read the Evidence

1. `profile.json` — shape in `../query-app-map/references/schema.md` ("profile.json"). Start
   with `technologies` (name, category, versions, confidence, evidence), `features` (resource,
   flow keys, routes, endpoints, forms, roles), `layout`, `data_model`, `data_setup`,
   `accounts`, `coverage`, `environments`, `differences`, `could_not_determine`, `warnings`.
2. `APP-MAP.md` of each run — flows (with the names from `names.yaml`), forms and their
   purposes, role access matrix, unexercised surface.
3. Drill in only where a sentence needs support: `appmap query --run <run> flows`,
   `appmap query --run <run> endpoint-shape --method GET --path '/api/items/{id}'`, or read-only
   `appmap query --run <run> sql "SELECT id, method, url, status FROM exchanges WHERE id = 42"`.
   Recipes and SQL: `../query-app-map/SKILL.md`.

Every evidence ref is `{run_id, kind, id}` with `kind` one of `exchange`, `state`, `file`,
`route`, `endpoint`, `flow`, `form`; resolve it in that run's map.

---

## Step 4 — Judge

Work through `references/profile-guide.md`:

- **Purpose** — what the application is for and who uses it, inferred from titles, navigation,
  feature set, roles and entity names. Two to four sentences, with refs.
- **Features** — `features` are clusters the command proposed (one per primary resource plus the
  flows touching it). Name each in the user's words ("Manage projects", not "Projects"); write a
  one- or two-sentence description (who does it, what changes) and a **how it works**: entry
  point, pages, forms, API calls in order, values passed between steps, preconditions — every
  step with refs.
- **Technology claims** — check each against its evidence snippets. If the evidence is weaker
  than the confidence suggests (a generic cookie name, a marker that could belong to something
  else), treat it as one level lower in your prose and report it. Never present a technology,
  version or confidence higher than the evidence supports, and never add one without a
  detector hit. `profile.json` is regenerated on every run, so record doubts in your report
  (and in the affected feature's "how it works" where it matters), not by editing the file.

Never write judgments: no "outdated", "vulnerable", "insecure", "end-of-life", CVE or risk
wording anywhere.

---

## Step 5 — Fill profile-names.yaml

```yaml
purpose: ""
features:
- key: ft-622b14a694df        # stable match key - do not edit
  name: Admin                  # pre-filled from the suggestion; replace it
  suggestion: Admin            # what the command proposed - do not edit
  description: ""
  how_it_works: ""
stale: []                      # names whose feature key no longer exists
```

- Keep `key` and `suggestion` as generated; fill `purpose`, `name`, `description`,
  `how_it_works`. Empty values are ignored. Multi-line prose: use a YAML block scalar (`|`).
- A re-run never overwrites your text; new features are appended. When a feature's key changes
  (its flows or resource changed after a new crawl), your entry moves to `stale` — re-map it to
  the new key and list it in your report; never drop it silently.
- Cite refs inline in the format of `references/profile-guide.md` (e.g. `[exchange 42]`,
  `[flow 3]`).

---

## Step 6 — Re-run and Review

Run the same `appmap profile` command again (same `--run`s and `--out`). `next` is `null` once
the purpose and every feature name are filled. Read `APP-PROFILE.md` top to bottom: purpose and
features first, technology table, layout, data model, data setup, test accounts, environments
(and differences), coverage orientation, "Could not determine", notes. Fix the YAML and re-run
if anything reads wrong.

---

## Step 7 — Report

One paragraph: what the application is, its main features, its stack (with confidence where not
confirmed), the environments covered, and the path to `APP-PROFILE.md`. Then list what the
profile **could not determine** (from `could_not_determine`, warnings, and your own gaps — e.g.
features you could not name, technology claims you doubt, stale names you re-mapped). For
production inputs repeat the handling notice: the files may contain customer data; store them
encrypted, share only with authorized people, delete them when the engagement ends.

---

## Completion Criteria

- [ ] Inputs checked; anything missing (unbuilt run, unfinished crawl, failed role) stated.
- [ ] `appmap profile` ran with status `profiled` or `partial` (and you know why it is partial).
- [ ] `purpose` and every feature `name`, `description` and `how_it_works` filled from evidence,
      with refs on every factual sentence — or deliberately left blank and reported.
- [ ] Stale names re-mapped or reported; none silently dropped.
- [ ] Technology claims checked against evidence; no confidence raised without evidence.
- [ ] No outdated/vulnerable/CVE/risk judgments written anywhere; no credentials, tokens or
      env-var values in any file or message.
- [ ] Re-ran after editing `profile-names.yaml` (`next` is `null`); `APP-PROFILE.md` reviewed.
- [ ] Report delivered with the "could not determine" list and, for production, the handling
      notice.

---

## Reference Files

- `references/profile-guide.md` — confidence rules, evidence-citing format, how to infer purpose
  and features, what never to write.
- `references/fingerprints.md` — how technology detection works, the rule format, adding a
  detector.
- `../query-app-map/SKILL.md` — recipes and read-only SQL for drilling into evidence.
- `../query-app-map/references/schema.md` — the run schema and the `profile.json` shape.
