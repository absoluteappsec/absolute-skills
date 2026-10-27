# absolute-skills

Agent skills from [Absolute AppSec](https://absoluteappsec.com) for understanding and testing **running web applications**. They work with any skill-aware coding agent (Claude Code, Codex, Gemini CLI, Cursor, …) — each skill is a `SKILL.md` the agent reads and follows.

Everything here lives under `dynamic/` because it works against a live, running application (as opposed to reviewing source code).

> Use these only against applications you own or are authorized to test.

## The lightweight skills — `dynamic/skills/`

Three self-contained skills, one file each, no install or tooling beyond whatever browser tool your agent already has (Playwright MCP, a browser extension, or a small Playwright script). They chain together:

```
crawl-site  →  SITE-MAP.md  →  summarize-site  →  SITE-PROFILE.md  →  create-test-plan  →  TEST-PLAN.md
```

| Skill | What it does | Output |
|---|---|---|
| [`crawl-site`](dynamic/skills/crawl-site/SKILL.md) | Crawls the site as each role you give it — pages, client-side routes, forms, buttons, API calls, and which role can reach what. Confirms authorization and scope first, blocks out-of-scope traffic, asks before destructive actions (deletes, password changes, logout, …), keeps credentials out of every file. | `SITE-MAP.md` |
| [`summarize-site`](dynamic/skills/summarize-site/SKILL.md) | Turns the crawl into a briefing for QA testers and developers: what the app is for, its features and how each works, tech stack with versions and confidence, layout, data model, data setup, test accounts, environments, coverage. Descriptive only — no judgments. | `SITE-PROFILE.md` |
| [`create-test-plan`](dynamic/skills/create-test-plan/SKILL.md) | Builds a prioritized security test plan: per-endpoint hypotheses (IDOR/access control, injection, auth bypass, logic abuse), scored by exposure, data sensitivity and evidence, mapped to OWASP Top 10 and ASVS, each handed off as a concrete Burp, ZAP or Playwright check. Plans only — sends no traffic. | `TEST-PLAN.md` |

**Using them:** copy the skill folders where your agent looks for skills (for Claude Code, `~/.claude/skills/` or a project's `.claude/skills/`), then ask in plain language:

1. "Crawl https://staging.example.com as admin and user" — give credentials via environment variables, never in chat.
2. "Summarize the site we crawled"
3. "Create a test plan for it"

## The full engine — `dynamic/absolute-appmap/`

For larger or repeated jobs, [`absolute-appmap`](dynamic/absolute-appmap/README.md) is a Claude Code plugin backed by a Python + Playwright CLI (`appmap`). It does the same crawl-and-understand work with a resumable crawler, an approval queue for destructive actions, multi-role cross-replay that builds an access matrix, API schema and JavaScript bundle discovery, and a queryable SQLite map. Its skills:

| Skill | Role |
|---|---|
| `map-running-application` | Orchestrator — runs the whole mapping workflow |
| `configure-crawl` | Scope, roles, profile (staging / throttled production), config file |
| `authenticate-roles` | Log in each role and save sessions |
| `crawl-application` | Browser crawl, including single-page apps |
| `probe-endpoints` | Robots/sitemaps, OpenAPI/GraphQL, JS bundle mining, cross-role replay |
| `review-approvals` | Present held destructive actions to the human and apply their decisions |
| `build-app-map` | Produce `APP-MAP.md`, `appmap.json` and the SQLite map |
| `query-app-map` | Answer questions from an existing map without re-crawling |
| `profile-running-application` | Briefing from an appmap run (`appmap profile` → `APP-PROFILE.md`) |

Install it from this repo's marketplace (`.claude-plugin/marketplace.json`), or see its [README](dynamic/absolute-appmap/README.md) and [usage guide](dynamic/absolute-appmap/docs/usage.md). `summarize-site` and `create-test-plan` can also read appmap output.

## License

[MIT](LICENSE) © 2026 Absolute AppSec.
