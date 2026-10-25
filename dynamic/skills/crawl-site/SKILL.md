---
name: crawl-site
description: Use when asked to crawl, spider or explore a running website or web app and write down everything it can do - pages, links, client-side routes, forms, buttons, API calls and what each user role can reach. Lightweight and tool-agnostic - drives whatever browser tool the agent has (Playwright MCP, a browser extension, or a short Playwright script) and records findings in SITE-MAP.md. Holds destructive actions (delete, password/email change, payments, sending messages, logout) for the user's OK. Triggers on "crawl this site", "explore the app at https://…", "map the pages and forms", "what can a logged-in user do on this site". For a heavier, resumable crawl use the absolute-appmap plugin instead.
---

## Purpose

Walk a running website the way a curious tester would, as each role you're given, and write down what exists: pages, routes, forms, buttons, the network/API calls behind them, and which role can reach what. The output, `SITE-MAP.md`, is the input for `summarize-site`.

This skill is a procedure for you, the agent. There is no special tooling: use the browser tool you have. Prefer, in order: a browser automation tool already available in your harness (e.g. Playwright MCP, a browser extension); otherwise write and run a small Playwright script; for static pages, plain HTTP fetches are fine.

## Ground rules (never break these)

1. **Authorization first.** Before any request, confirm with the user that they own or are authorized to test the site, and agree the scope (hosts and paths). Stay inside it; note but never visit out-of-scope links.
2. **Never perform destructive actions without asking.** Before clicking or submitting anything that deletes, deactivates, changes a password or email, pays, transfers, sends email/SMS/messages, invites, or logs out, stop and list it for the user. Do it only if they say yes, one batch at a time. When unsure, ask.
3. **Credentials stay out of files and chat.** Use credentials only to log in. Never write passwords, session cookies or tokens into `SITE-MAP.md` or echo them back. Write `<redacted>` instead.
4. **Be gentle.** One page at a time, a short pause between actions. If you see errors pile up (429, 5xx), slow down or stop and tell the user. Production sites: ask before submitting any form at all.
5. **Generated test data is labelled.** When you fill forms, use values containing a marker such as `sitemap-test-<date>` so records can be found and cleaned up later. List every record you create.

## Step 1 — Set up

Ask (or confirm from the request): base URL(s), in-scope hosts/paths, anything to avoid (e.g. `/logout`, admin user pages), whether it's staging or production, and the roles to crawl with how to log in as each (the user types credentials or tells you where they are — don't store them).

Create `SITE-MAP.md` in the working directory (or where the user says) with a header: site, date, scope, roles, environment.

## Step 2 — Crawl each role

For each role (start with anonymous if there is one), log in, then explore breadth-first from the landing page:

- **Pages:** visit every in-scope link. Record URL, title, a one-line description of what the page is for, and how you got there.
- **Client-side routes (single-page apps):** watch for URL changes without page loads; check the page source/JS for a router route list; visit routes you find but no link shows (note them as "found in code").
- **Interactive controls:** menus, tabs, buttons, cards and anything clickable. Click non-destructive ones; note what they reveal or what request they send.
- **Forms:** record fields (name, type, required, options), the action/method, and what happens on submit. Fill with sensible labelled test data and submit unless rule 2 applies. Record validation errors you hit — they describe the rules.
- **Network:** watch the network log. Record each distinct API call: method, path (replace ids with `{id}`), what triggered it, status, and the shape of the response (field names, not full data). Note GraphQL operation names.
- **Free extras:** check `robots.txt`, `sitemap.xml`, and common API docs paths (`/openapi.json`, `/swagger.json`, `/schema/`, `/swagger-ui/`, `/api/docs/`). Record what you find.
- **Errors and tells:** note error pages, debug pages, framework banners, server headers — useful for the summary.

Don't revisit near-identical pages endlessly: for lists and detail pages, sample a few items, then move on. Keep going until no new pages, routes, forms or API calls appear.

## Step 3 — Cross-check roles

For each page and API call one role reached, try it as the other roles (a plain visit or the same GET; ask before re-sending any non-GET). Record the outcome per role: works / redirected to login / forbidden / not found / different content.

## Step 4 — Write SITE-MAP.md

Use these sections (tables where it helps). Keep it factual — what you saw, not judgments.

```
# Site map: <site>
Date · Scope · Environment · Roles crawled

## Pages and routes        (URL pattern · title · purpose · roles that reach it · found via)
## Forms                   (page · purpose · fields · submits to · result / validation seen)
## API calls               (method · path · triggered by · status · response shape)
## Flows                   (numbered steps for each meaningful journey, e.g. sign up, create X, edit X)
## Role access matrix      (page or API × role → works / login / forbidden / …)
## Technology clues        (headers, cookies, banners, error pages, libraries seen — raw evidence)
## Held actions            (destructive actions found, approved or not, and what happened)
## Records created         (test data you created, with markers)
## Not covered             (anything you couldn't reach or skipped, and why)
```

## Completion Criteria

- [ ] Authorization and scope confirmed with the user before the first request.
- [ ] Every role crawled until no new pages, routes, forms or API calls appear.
- [ ] Every destructive action was listed for the user before being performed (or skipped).
- [ ] Role access checked across roles.
- [ ] `SITE-MAP.md` written with all sections; no passwords, cookies or tokens in it.
- [ ] Told the user where the file is, what you created, and what wasn't covered. Suggest `summarize-site` next.

## Reference Files

None — this skill is self-contained.
