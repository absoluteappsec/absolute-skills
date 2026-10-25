---
name: summarize-site
description: Use after a site has been crawled to write a briefing that brings a QA tester or developer up to speed on a web application - what it is for, its main features, how each feature works, the language/framework/libraries and versions it runs on, its layout, data model, data setup, test accounts and environments - pointed at building full test coverage. Reads SITE-MAP.md from crawl-site (or an absolute-appmap run's APP-MAP.md / appmap.sqlite) and writes SITE-PROFILE.md. Descriptive only - no vulnerability or "outdated" judgments - and uses only what the running site showed, no source code. Triggers on "summarize the site we crawled", "what does this app do and how", "write an app profile for QA", "what tech is this site built on".
---

## Purpose

Turn crawl findings into context: a readable briefing someone new to the application can use to understand what it does, how it does it, and what they need to test it fully. You write it from the evidence the crawl recorded — nothing invented.

## Ground rules

1. **Evidence or silence.** Every factual statement comes from the crawl notes (a page, form, API call, header, banner…). If you're inferring, say so ("likely", "appears to"). If you can't tell, put it under "Could not determine".
2. **Descriptive only.** Describe what is there. Don't call anything outdated, vulnerable, insecure or risky — later tools do analysis.
3. **No secrets, careful with people.** Never copy passwords, tokens or cookie values. Don't list real users' names/emails from production data; the run's own test accounts are fine.
4. **No new requests.** Work from the crawl output. If something important is missing, list it and suggest re-crawling.

## Step 1 — Gather the input

Read `SITE-MAP.md` (from `crawl-site`). If the user has an `absolute-appmap` run directory instead, read its `APP-MAP.md` (and, if helpful, query `appmap.sqlite` read-only, e.g. `sqlite3 -readonly appmap.sqlite`), plus its `APP-PROFILE.md` if `appmap profile` was run. If neither exists, tell the user to crawl first (`crawl-site` or `absolute-appmap`).

## Step 2 — Work out the technology

From the technology clues, identify language, server framework, front-end framework, libraries, web/app server, CDN, auth style and API style. For each, note the version if a banner, header or error page shows it, and how sure you are:

- **confirmed** — named explicitly (e.g. `Server: gunicorn`, a `/*! jQuery v3.7.1` banner, a Django debug page, an OpenAPI `info` block), or two independent clues agree.
- **likely** — one indirect clue (e.g. a `csrftoken` cookie suggests Django) or an inference (Django ⇒ Python).
- **guessed** — your inference without a direct clue.

Common tells: cookie names (`csrftoken`/`sessionid` Django, `JSESSIONID` Java, `laravel_session`, `_rails_session`, `connect.sid` Express, `PHPSESSID`), headers (`Server`, `X-Powered-By`), script names/banners (`vue.global.prod.js`, `react-dom`, `jquery`, `bootstrap`), DOM markers (`__NEXT_DATA__`, `ng-version`, `data-reactroot`), admin consoles (`/admin/` Django), API docs (drf-spectacular, Swagger UI, GraphQL).

## Step 3 — Work out purpose, features and how they work

- **Purpose:** from page titles, headings, navigation labels, form names and API resource names, say in 2–3 sentences what the application is for and who uses it.
- **Features:** group pages, forms and API calls into user-facing features (e.g. "Projects: create, view, edit, delete"). For each: what it does, which roles can use it, entry point.
- **How it works:** for each feature, the step-by-step flow — pages visited, forms submitted, API calls made in order, and data passed between steps (e.g. "the id returned by POST /api/items is used in GET /api/items/{id}").

## Step 4 — Data, accounts, environments, coverage

- **Data model & types:** entities and their fields, types, required fields, allowed values and id formats — from API schemas if found, otherwise from response shapes and form fields.
- **Data setup:** what must exist before each flow works (e.g. "editing a task needs an existing project"), seed data observed, records the crawl created.
- **Test accounts:** each role crawled — login method and what it could reach. Test accounts the crawl created. (No passwords.)
- **Environments:** which URL/environment the crawl covered and when; differences if several were crawled.
- **Coverage orientation:** a features × roles grid, plus what the crawl did not reach or skipped (held actions, failed logins, unexplored areas).

## Step 5 — Write SITE-PROFILE.md

```
# Application profile: <app>
Crawled <date> · <environment(s)> · roles: …

## Purpose
## Main features            (feature · what it does · roles · entry point)
## How it works             (per feature: numbered flow incl. API calls and data passed)
## Technology stack         (name · category · version · confidence · evidence)
## Layout                   (areas/sections, page hierarchy, API groups, how UI and API connect)
## Data model & types
## Data setup
## Test accounts
## Environments
## Coverage orientation     (features × roles; not covered)
## Could not determine
```

Lead with purpose and features — a reader should know what the app is within the first screen.

## Completion Criteria

- [ ] Every section filled, or its gap listed under "Could not determine".
- [ ] Every technology has a confidence level and its evidence.
- [ ] Every feature has a "how it works" flow.
- [ ] No vulnerability/outdated judgments; no secrets; no production users' personal data.
- [ ] Told the user where `SITE-PROFILE.md` is and the main things it couldn't determine.

## Reference Files

None — this skill is self-contained.
