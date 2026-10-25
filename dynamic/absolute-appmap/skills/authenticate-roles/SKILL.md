---
name: authenticate-roles
description: Use during an appmap run, after appmap init, to log in every configured role and save its session - scripted form logins from environment-variable credentials, manual SSO/MFA logins the operator completes in a headed browser, saved Playwright storage state, and anonymous roles - then verify each role authenticated (auth_ok) and troubleshoot failures without locking accounts. Triggers on "log in the roles", "authenticate admin and user", "the SSO role needs a manual login", "appmap auth failed", "session expired".
---

## Purpose

Every role must hold a working session before probing and crawling. `appmap auth` logs each
role in, records the outcome in the `roles` table (`auth_ok`), and saves the session (cookies,
localStorage, sessionStorage) to `~/.cache/absolute-appmap/sessions/<run_id>/<role>.json` —
never into the run directory. Later commands reuse it. When a session is lost mid-crawl or
mid-probe, `form` roles are re-logged in automatically; a `manual` role would need the operator
again, and that re-login prompts on the terminal — which is why `crawl` and `probe` are run with
stdin closed when a manual role exists (see `crawl-application`), so a lost manual session fails
fast instead of waiting.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`.

---

## Step 1 — Check Prerequisites

1. Read `<run>/run.json` → `config.roles` to list each role and its `login.type`.
2. `form` roles: the variables named in `username_env` / `password_env` must be set in the
   environment your commands run in. Check presence only, e.g.
   `test -n "$APPMAP_ADMIN_PASS" && echo set || echo MISSING`. Never print the values. If one is
   missing, ask the operator to set it (never to paste it into chat).
3. `storage_state` roles: the file at `storage_state_path` (relative to the config file) exists.
4. `manual` roles: the operator is available, at a machine with a display, to log in by hand.

---

## Step 2 — Log In the Scripted Roles

If no role is `manual`, run all roles at once:

`appmap auth --run <run>`

If some roles are `manual`, run each non-manual role on its own so the command never waits for
keyboard input: `appmap auth --run <run> --role <role>` (for `form`, `storage_state` and `none`
roles).

Read the status line: `status: "authenticated"` with `counts.roles_ok == counts.roles` is
success; `auth_failed` lists the failing roles in `errors`; `error` means the command itself
failed (unknown role, unreadable run directory).

---

## Step 3 — Manual Logins (SSO, MFA, CAPTCHA)

A manual login opens a **headed** browser and then waits for Enter on the terminal, so the
operator runs it in their own terminal:

1. Give the operator the exact command, with absolute paths:
   `/path/to/absolute-appmap/scripts/appmap auth --run <run> --role <role>`
2. Tell them: a browser window opens at the login page; log in as `<role>` (complete SSO/MFA);
   when the app shows the logged-in page, return to the terminal and press Enter; then paste you
   the final `APPMAP_STATUS` line.
3. **Ask the operator to confirm they actually reached the logged-in page before pressing
   Enter.** A manual login is recorded as successful (`auth_ok = 1`) whenever Enter is pressed —
   the CLI cannot verify it — so the operator's confirmation is the only check. If they are unsure,
   repeat the login.
4. A `form` role blocked by MFA or a CAPTCHA can be logged in the same way once:
   `appmap auth --run <run> --role <role> --manual-login`. (Without `--role`, `--manual-login`
   turns every form and manual role into a manual login.)

If your harness can drive an interactive terminal, you may run the command yourself and let the
operator type in the browser — but never type credentials on their behalf.

---

## Step 4 — Verify Every Role

Run `appmap query --run <run> sql "SELECT name, login_type, auth_ok FROM roles ORDER BY name"`.

Every role must show `auth_ok = 1` (`none` roles count as authenticated; `manual` roles count as
authenticated once Enter was pressed, so rely on the operator's confirmation from Step 3). `0` = the last login
failed; `NULL` = never attempted.

---

## Step 5 — Troubleshoot Failures (at most two attempts per role)

Repeated failed logins can lock real accounts. After two failures for a role, stop and ask the
operator.

| Symptom | Likely cause | Fix |
|---|---|---|
| Fails immediately | Env var not set in this environment | Operator sets it; rerun that role |
| Fails immediately (`login failed: … invalid URL` error event in `events.jsonl`) | Relative login `url` such as `/login` | Use the absolute login page URL (`https://host/login`) |
| Fails after ~5–10 s | Wrong login `url` (no password field there), or success never detected | Check the URL; see the next rows |
| Fields not found / wrong field filled | Non-standard login form | Add `username_selector`, `password_selector`, `submit_selector` to the role's `login` |
| "Succeeds" but the app is still anonymous | Login stays on the same URL | Add `success_selector` (an element only visible when logged in) |
| MFA prompt, CAPTCHA, SSO redirect | Not scriptable | Step 3 (manual), or `type: manual` in the config |
| `storage_state` fails | File missing or expired | Ask the operator for a fresh state file |
| `crawl`/`probe` hangs after printing `[appmap] Log in as role …` | A manual role's session expired and the command is waiting for Enter | Stop the command; re-authenticate that role with the operator (Step 3); rerun with stdin closed (`</dev/null`) |

Edit the config file in place (it is re-read on every command), then rerun
`appmap auth --run <run> --role <role>`.

Later in the run: if `crawl` or `probe` report a role's login failed or its saved session looks
expired, rerun this skill for that role — manual roles always need the operator again
(`appmap auth --run <run> --role <role> --manual-login`).

---

## Completion Criteria

- [ ] Every `form` role's environment variables confirmed present (values never shown).
- [ ] Scripted roles authenticated with `appmap auth`; manual roles completed by the operator.
- [ ] `roles` table shows `auth_ok = 1` for every role, or the operator agreed to drop a role
      (remove it from the config so later commands skip it).
- [ ] No role exceeded two failed login attempts without the operator's go-ahead.

Return to `map-running-application` Step 3 (`probe-endpoints`, passive).

---

## Reference Files

- `../map-running-application/references/config-reference.md` — login types, selectors,
  `storage_state_path`, where sessions are stored.
- `../map-running-application/references/safety.md` — credential handling rules.
