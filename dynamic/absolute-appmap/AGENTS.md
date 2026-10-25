# AGENTS.md — absolute-appmap

Entry point for coding agents that do not load Claude Code plugins (Codex, Gemini CLI, Cursor,
and others). Everything here is plain Markdown plus one command-line tool; no MCP server or
vendor-specific tool is needed.

## What this plugin does

It crawls a **running** web application (server-rendered or SPA) as every configured user role,
using Playwright plus direct HTTP requests, and records everything in an open, documented run
directory: `appmap.sqlite`, `appmap.json`, `APP-MAP.md`, `events.jsonl`, per-role HAR files,
screenshots. Destructive actions are held for human approval. The result feeds test generation
(browser E2E, HTTP/API, unit) and security testing.

## Where to start

1. To map an application: read and follow **`skills/map-running-application/SKILL.md`**. It
   calls the phase skills in order:
   `configure-crawl` → `authenticate-roles` → `probe-endpoints` (passive) → `crawl-application`
   → `probe-endpoints` (active) → `review-approvals` → double back → `build-app-map`.
2. To brief someone on an already-mapped application (purpose, features, technology stack,
   layout, data model, test accounts, environments): read
   **`skills/profile-running-application/SKILL.md`**. It runs `appmap profile` and writes
   `APP-PROFILE.md` — the dynamic counterpart of a source-code profile.
3. To use an existing run directory (e.g. to write tests from it): read
   **`skills/query-app-map/SKILL.md`**. Read the run's `APP-PROFILE.md` first when there is one.

Each skill is `skills/<name>/SKILL.md`; detail lives in its `references/` folder and loads on
demand. "Invoke skill: `x`" means read `skills/x/SKILL.md` and follow it.

## The CLI

Run every command through the wrapper **`scripts/appmap`** (absolute path). On first use it
creates a virtualenv and installs Chromium under `~/.cache/absolute-appmap/` (needs Python 3.11+
and network). Subcommands: `init`, `auth`, `crawl`, `probe`, `approvals`, `build`, `status`,
`profile`, `query`; `scripts/appmap <subcommand> --help` lists the options.

## The APPMAP_STATUS contract

Every subcommand ends with one machine-readable line on stdout:

```
APPMAP_STATUS {"command": "...", "ok": true, "status": "...", "counts": {...}, "pending_approvals": 0, "errors": [], "next": "..."}
```

- Branch on `status`. `crawl` and `probe` stop after a time budget (default 8 minutes) with
  `"in_progress"`: rerun the same command until the status changes.
- `"paused_circuit"`: the target is failing or rate limiting — stop and tell the operator.
- A form role whose last login failed is skipped by `crawl`/`probe` (warning in `errors`);
  only `auth` retries it, after the operator fixed the credentials (lockout risk).
- `"auth_failed"` (crawl): a role's login failed and it was stopped — rerun `auth` for it (the
  operator logs in for manual roles), never retry logins in a loop.
- `"locked"`: another command is running on the same run directory — run one at a time.
- `next` suggests the next command (written as bare `appmap …`; run it via `scripts/appmap`).
- All status values: `skills/map-running-application/references/methodology.md`.

## Safety rules (non-negotiable)

- **Authorization:** never start a run until the human operator has explicitly confirmed they
  are authorized to test the target and has confirmed the scope. Record it in the config.
- **Approvals:** never approve, deny or skip a held action yourself. Present the queue to the
  human, record only their decisions in `approvals.yaml`, then run
  `scripts/appmap crawl --run <run> --approved-only`.
- **Credentials:** only via environment variables named in the config, or the operator's manual
  login. Never ask for, print or store a password.
- **No side channels:** do not send requests to the target or drive a browser outside the CLI;
  its scope, throttle, approval and redaction gates must see every request.
- **Sensitive output:** run directories can contain production customer data. Do not commit or
  share them beyond what the operator asks.

Details: `skills/map-running-application/references/safety.md`.
