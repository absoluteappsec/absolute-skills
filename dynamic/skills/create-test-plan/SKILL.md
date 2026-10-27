---
name: create-test-plan
description: Use after a web application has been crawled and profiled to turn the map into a prioritized security test plan - generate per-endpoint hypotheses (IDOR / broken access control, injection, authentication and session bypass, business-logic abuse), score each by exposure, data sensitivity and supporting evidence, map them to OWASP Top 10 and ASVS controls, and hand every pathway off as a concrete check for Burp Suite, OWASP ZAP or Playwright. Reads SITE-MAP.md / SITE-PROFILE.md (crawl-site, summarize-site) or an absolute-appmap run, writes TEST-PLAN.md. Plans only - does not send attack traffic. Triggers on "create a test plan", "what should we test on this app", "build a pentest plan from the site map", "map the attack surface to OWASP/ASVS", "give me Burp/ZAP/Playwright checks for this app".
---

## Purpose

Turn what the crawl saw into a ranked list of things worth testing, each one specific enough that a tester (or a tool) can run it without re-reading the whole map. Every hypothesis is tied to a real endpoint and real evidence, scored, mapped to OWASP Top 10 and ASVS, and ends in a concrete check.

This skill **plans**; it does not test. It sends no requests to the target. Running the checks is a separate, authorized step.

## Ground rules

1. **Evidence-based.** Every hypothesis names the endpoint/page/form and the observation that suggests it (e.g. "`/taskManager/profile/{id}` edit form opened for any id as user"). No generic checklist padding — if there's no evidence, it's at most a low-priority "standard coverage" item.
2. **Authorized scope only.** Use the scope recorded in the site map. Note any check that would touch out-of-scope hosts (e.g. SSRF callbacks, external fetches) and require explicit approval for it.
3. **Flag destructive checks.** Mark any check that changes or deletes data, changes credentials, sends messages, or could disrupt the service, and say it needs approval and should target test records only. Carry over any standing rules from the crawl (held actions, accounts never to touch) as constraints. List the test fixtures checks need (e.g. a second ordinary user, a project the user isn't a member of, a marker string) under Preconditions so they're created once, deliberately.
5. **Scanner safety.** Any ZAP/Burp spider or active-scan config must exclude state-changing GETs (delete/complete/logout-style URLs) and block out-of-scope hosts the pages call on their own (telemetry, CDNs); default to low attack strength.
4. **No secrets.** Never copy passwords, tokens, cookies or API keys into the plan. Use placeholders (`{{ADMIN_SESSION}}`, `{{USER_TOKEN}}`).

## Step 1 — Gather inputs

Read, in this order of preference: `SITE-PROFILE.md` and `SITE-MAP.md` (from `summarize-site` / `crawl-site`), or an absolute-appmap run (`APP-MAP.md`, `APP-PROFILE.md`, `appmap.sqlite` read-only — `sqlite3 -readonly`). Collect: endpoints/pages with methods and parameters, forms and fields, roles and the role access matrix, flows, IDs/object references, tech stack, auth mechanism, observations (errors, debug pages, held actions). Note the OWASP Top 10 and ASVS versions to use — default **OWASP Top 10:2025** and **ASVS 5.0** unless the user says otherwise.

## Step 2 — Generate hypotheses per endpoint

For each endpoint/page/form (group near-identical ones), ask each question below and write a hypothesis only where the evidence supports it:

- **Access control / IDOR:** object IDs in paths, query or bodies; pages one role reached that another shouldn't; admin functions reachable or partially reachable by lower roles; horizontal access (user A → user B's objects); function-level access (non-admin calling admin actions directly, including POSTs the UI hides); mass assignment (extra fields like `is_staff`, `role`, `owner`).
- **Injection:** parameters reflected in responses or passed to queries, shells, templates, URLs, files or headers — SQL/NoSQL, OS command, template, XSS (reflected/stored/DOM), SSRF (URL-taking fields), path traversal (file/path parameters), header/CRLF, deserialization.
- **Authentication & session:** login, registration, password reset/change, tokens (JWT/opaque), cookies (flags, scope), session fixation/expiry, MFA, username enumeration, rate limiting/lockout, token in URL, CSRF on state-changing requests.
- **Business-logic abuse:** state-changing GETs, skipped workflow steps, replaying or reordering requests, negative/oversized values, race conditions on create/redeem, approval or assignment flows a role shouldn't drive, limits and quotas.
- **Configuration & exposure (where evidenced):** debug pages, verbose errors/stack traces, exposed docs/schemas, environment dumps, security headers, weak hashing or crypto noted in the profile.

Each hypothesis: **ID**, **endpoint(s)**, **hypothesis** (one sentence), **evidence** (what in the map supports it, with the source section), **roles** involved.

## Step 3 — Score each hypothesis

Score 1–3 on each:

| Factor | 1 | 2 | 3 |
|---|---|---|---|
| **Exposure** | admin-only | any authenticated user | anonymous / internet-facing |
| **Data sensitivity** | public/low-value | internal business data | PII, credentials, tokens, financial, admin/privilege |
| **Evidence** | standard coverage, no specific signal | indirect signal (pattern, ID in URL, tech known to allow it) | directly observed behavior (e.g. other user's object returned, input reached a shell) |

**Priority = sum (3–9):** P1 = 8–9, P2 = 6–7, P3 = 3–5 — but an item with Evidence = 1 is capped at P3. Sort by priority, then evidence, then exposure; when there are many P1s, pick the "run first" five by directly observed evidence on anonymous or cross-role paths.

## Step 4 — Map to OWASP Top 10 and ASVS

Tag each hypothesis with the OWASP Top 10 category and the ASVS chapter/requirement area it tests. Default references (verify against the official lists if you have web access; use the user's versions if they specify):

- **OWASP Top 10:2025:** A01 Broken Access Control · A02 Security Misconfiguration · A03 Software Supply Chain Failures · A04 Cryptographic Failures · A05 Injection · A06 Insecure Design · A07 Authentication Failures · A08 Software or Data Integrity Failures · A09 Security Logging and Alerting Failures · A10 Mishandling of Exceptional Conditions.
- **ASVS 5.0 chapters:** V1 Encoding and Sanitization · V2 Validation and Business Logic · V3 Web Frontend Security · V4 API and Web Service · V5 File Handling · V6 Authentication · V7 Session Management · V8 Authorization · V9 Self-contained Tokens · V10 OAuth and OIDC · V11 Cryptography · V12 Secure Communication · V13 Configuration · V14 Data Protection · V15 Secure Coding and Architecture · V16 Security Logging and Error Handling · V17 WebRTC.

Cite specific ASVS requirement IDs only when you're sure of them; otherwise cite the chapter. Note that in the 2025 Top 10, SSRF and CSRF fall under A01 Broken Access Control.

## Step 5 — Hand off each pathway as a concrete check

For every P1/P2 hypothesis (P3 optional), write one check in the best-fit tool, with **preconditions** (roles/sessions/test data), **steps**, **expected secure result**, and **finding condition**:

- **Burp Suite** — for request tampering, IDOR and role swaps: the raw HTTP request (with `{{PLACEHOLDER}}` auth), the parameter(s) to change and the values to try; for role checks, say "replay with `{{USER_SESSION}}` (e.g. Autorize/AuthMatrix, or Repeater with swapped cookie)"; for fuzzing, Intruder positions and payload set (name the list type, don't paste huge lists).
- **OWASP ZAP** — for broad injection/config scanning: target URLs or context (include/exclude regexes from scope), authentication method and users per role, scan policy focus (e.g. SQLi, XSS, path traversal), and an Automation Framework snippet (YAML) when useful.
- **Playwright** — for multi-step flows, business logic and UI-driven access control: a short test (TypeScript or Python) that logs in as the role (credentials from env vars), performs the steps, and asserts the secure outcome.

Mark destructive checks `⚠ needs approval — test records only`.

## Step 6 — Write TEST-PLAN.md

```
# Security test plan: <app>
Date · Inputs (sources) · Scope · Roles · OWASP Top 10 / ASVS versions

## Summary            (counts by priority and category; top 5 to run first)
## Hypotheses         (table: ID · priority · score E/S/Ev · endpoint · hypothesis · OWASP · ASVS · tool)
## Checks             (per hypothesis: preconditions · steps · expected · finding condition · tool snippet)
## Coverage map       (OWASP Top 10 categories × hypotheses; ASVS chapters touched / not touched)
## Needs approval     (destructive or out-of-scope checks)
## Gaps               (areas the map didn't cover that limit the plan — e.g. roles not crawled)
```

## Completion Criteria

- [ ] Every hypothesis names a real endpoint and cites evidence from the inputs.
- [ ] Every hypothesis is scored (E/S/Ev) and prioritized.
- [ ] Every hypothesis maps to an OWASP Top 10 category and an ASVS chapter.
- [ ] Every P1/P2 hypothesis has a concrete Burp, ZAP or Playwright check with expected and finding conditions.
- [ ] Destructive/out-of-scope checks are flagged; no secrets in the plan.
- [ ] Told the user where `TEST-PLAN.md` is and the top checks to run first.

## Reference Files

None — this skill is self-contained.
