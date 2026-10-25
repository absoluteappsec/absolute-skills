---
name: review-approvals
description: Use during an appmap run whenever actions are held for approval (pending_approvals above zero) - refresh approvals.yaml, review each held destructive or data-creating action (delete, credential change, account change, payment, outbound message, deactivate, logout, production writes), confirm or downgrade the heuristic flag with reasons, look for dangerous actions the heuristics missed, present the batch to the human grouped by category, record only the human's decisions, apply them and execute approved actions with appmap crawl --approved-only. Triggers on "review the approval queue", "what needs approval", "approve the deletes of test records", "revoke that approval", "pending approvals".
---

## Purpose

The crawler never performs a held action on its own. Held actions wait in the approval queue
(`approvals` table, mirrored in `<run>/approvals.yaml`) while the crawl continues elsewhere. This
skill turns that queue into **human** decisions: you do the analysis and recommendation; the
human decides; the CLI executes exactly what was approved.

You never approve, deny or skip on the human's behalf, and you never write a rule they did not ask
for. Undecided items simply stay `pending` and appear in the map as known-but-unexercised surface.

Conventions (`appmap` = the plugin's `scripts/appmap` wrapper, run by absolute path; the
`APPMAP_STATUS` contract) are in `../map-running-application/SKILL.md`.

---

## Step 1 — Refresh the Queue

`appmap approvals --run <run> write`

Rewrites `<run>/approvals.yaml` from the database, keeping existing decisions, notes and rules.
Status `written`; `counts`: `pending`, `approved`, `denied`, `skipped`, `executed`,
`approved_unexecuted`, `failed`. `appmap approvals --run <run> list` prints just the pending
items as JSON lines.

The file:

```yaml
items:
- id: ap-0003
  role: admin
  category: delete
  label: Delete project
  reasons: ["label matches delete keyword 'delete'"]
  predicted_request: {method: POST, url: https://staging.example.com/projects/17/delete}
  created_by_run: true         # only present when set: found on a record this run created
  decision: pending            # pending | approve | deny | skip
  note: ''                     # free text; kept across rewrites
  execution: executed (ev-000812)   # informational, appears once run or failed
rules: []
```

A `pending` item with `execution: failed: target moved/predicted request not observed` was
approved before, but when it ran its control no longer acted on that target (a list shifted
after an earlier delete, or the row is gone): nothing was executed and it went back to
`pending`. Rules never re-approve such an item; present it again and only the human's new
`approve` retries it.

The queue does not grow when a list shifts: an action with the same role, label and target
request (method and URL; a GET only when it is the action itself) as an existing item, in any
state, reuses that item.

---

## Step 2 — Review Each Item (your analysis)

For every `pending` item, gather context and form a recommendation:

1. Where it is: `appmap query --run <run> sql "SELECT id, state_id, action_path, predicted_request FROM approvals WHERE id = 'ap-0003'"`;
   the screenshot `<run>/artifacts/<state_id>.png` and DOM `<run>/artifacts/<state_id>.html`.
2. What it would touch: does the predicted URL or label reference a record **this run created**
   (`created_by_run: true`, the run marker `appmap-<run_id>` in it, or an id returned by one of
   this run's writes)? Or pre-existing/shared data?
3. Side effects: emails/SMS/invites to real people, payments, external integrations, account
   lockout or deactivation, loss of the role's own access. For `account_change` /
   `credential_change` items, find **whose** account it is (the id in the URL, the form's
   prefilled username): the crawler would submit generated values into it.
4. Reversibility and environment: staging vs production (`run.json` → `profile`).

Then classify, always with a one-line reason (see `references/destructive-heuristics.md`):

- **Confirm** the flag — genuinely destructive/irreversible; recommend `deny` (or `approve` only
  for this run's own test records on staging).
- **Downgrade** — a heuristic false positive (e.g. "Send" on a search form, "Remove filter");
  recommend `approve`, explaining why it is safe.
- **Skip** — irrelevant to the map or duplicate of another item; recommend `skip`.

**Missed by the heuristics:** look for dangerous actions that were *not* held:

- Already performed: `appmap query --run <run> --format table sql "SELECT t.role, t.action_type, t.action_label, x.method, x.url, x.status FROM transitions t JOIN exchanges x ON x.id IN (SELECT value FROM json_each(t.exchange_ids)) WHERE upper(x.method) NOT IN ('GET','HEAD','OPTIONS') ORDER BY t.id"`.
  If something harmful already ran (e.g. sent a real email, published content), tell the
  operator **immediately**.
- Not yet performed: pending crawl items whose labels look dangerous:
  `appmap query --run <run> sql "SELECT role, url, action_path FROM frontier WHERE status = 'pending'"`.
  The CLI has no way to add items to the queue; to keep such an action from running, propose a
  `deny_patterns` entry for the config (a scope change — the operator decides).

---

## Step 3 — Present the Batch to the Human

Group by `category` (logout, delete, credential_change, account_change, payment,
outbound_message, deactivate, data_create). For each group show a compact table — id, role, label, predicted request, why it was
held, **your recommendation and reason** — and ask for a decision per item or per group.

- Say plainly that unanswered items stay pending (unexercised) and that is acceptable.
- Logout items: tell them these are never executed even if approved; `skip` is the natural answer.
- Offer batch rules only as an option, spelled out exactly, e.g. "approve every delete of a
  record this run created" →
  `{match: {category: delete, created_by_run: true}, decision: approve}`. Records are usually
  addressed by numeric ids, so `created_by_run` (set when the crawler queued the action from the
  page a successful create landed on) is the reliable key; `url_contains: appmap-<run_id>` only
  matches slug URLs that carry the marker. Match keys are `category`, `role`, `url_contains`,
  `label_contains` (strings) and `created_by_run` (`true`/`false`); all must match; the first
  matching rule wins; rules never change an item that already has a decision.
- **Tell the human explicitly, before they agree to a rule:** a rule also decides matching items
  that are queued **later** in this run — they will be approved (and executed by the next
  `--approved-only` crawl) without being shown individually. Offer per-item decisions instead if
  they would rather see each one.
- Never present your recommendation as the decision; wait for the human's answer.

---

## Step 4 — Record the Human's Decisions and Apply

1. Edit `<run>/approvals.yaml`: set each answered item's `decision` to `approve`, `deny` or
   `skip`, and put in `note` who decided and why (e.g. `operator: approve — test record created by
   this run`). Add only the rules the human asked for under `rules:`. Leave unanswered items
   `pending`.
2. `appmap approvals --run <run> apply` → status `applied`; `counts.changed` = decisions taken
   in; `errors` lists malformed decisions or rules (`approvals.yaml: rule #2 skipped: …`) — fix
   and apply again.
3. **Revoking** an approval that has not executed yet: set its decision to `deny`. Do not set it
   back to `pending`: a matching rule could approve it again on the next load. Executed items
   cannot be undone.

---

## Step 5 — Execute Approved Actions

If `counts.approved_unexecuted > 0`:

1. `appmap crawl --run <run> --approved-only` — replays each approved item's action path and lets
   only that action's own request(s) through. Rerun with `--approved-only` while `in_progress`;
   `paused_circuit` → stop and tell the operator.
2. Read `counts.executed`, `counts.failed`, `counts.skipped` and `errors` (one line per item that
   failed, e.g. the control could not be found again, or a logout marked `skip`). An item that was
   interrupted mid-execution is recorded as executed with outcome unknown — never re-run.
   An item whose predicted request was not observed (the action sent nothing, or only a
   request to another record, which the guard blocked) or whose anchored list row is gone is
   failed with "target moved/predicted request not observed", **not** executed, and back to
   `pending` — report it to the human with the others at the next review. List-row controls
   whose only locators are positional carry a content anchor (the row's `data-*id`, href or
   text), so after one delete the next approved item still finds its own row.
3. Then a normal `appmap crawl --run <run>` to explore whatever the approved actions revealed
   (new pages, created records). Its new held actions come back to Step 1.

---

## Completion Criteria

- [ ] `approvals.yaml` refreshed; every pending item reviewed with a recommendation and reason.
- [ ] Missed dangerous actions checked; anything harmful already performed reported to the operator.
- [ ] Batch presented to the human grouped by category; only their decisions recorded (with notes).
- [ ] `appmap approvals … apply` run with no unresolved `errors`.
- [ ] Approved items executed with `--approved-only` until `done`; failures noted for the report.
- [ ] Follow-up normal crawl run; any new pending items cycled through this skill.

Return to `map-running-application` Step 7 (double back).

---

## Reference Files

- `references/destructive-heuristics.md` — what each category means, how actions are flagged,
  common false positives and false negatives, how to judge risk.
- `../map-running-application/references/safety.md` — approval, production and cleanup rules.
