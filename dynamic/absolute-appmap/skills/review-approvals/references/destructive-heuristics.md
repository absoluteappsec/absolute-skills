# Destructive-action heuristics

How the CLI decides to hold an action, and how you judge the result. The classifier runs before
every click, form submission and direct request (browser crawl and active probe alike).

## How an action is classified

The label, URL and surrounding text are tokenized (camelCase split, punctuation removed,
lowercased) so `deleteUser`, `Deleted` and `/api/sendEmail` match but `payload` does not match
`pay`. Checks run in this order; the first hit wins:

1. **logout** — `log out`, `logout`, `sign out`, `log off`, `sign off` in the label or URL.
2. **delete** — HTTP `DELETE`, always.
2a. **outbound_message** (account recovery) — only for form submissions and non-GET
   requests, label or URL only: forgot/forgotten password (or username/login), password
   reset request, resend verification/confirmation/activation email/link/code, send (me)
   a (password) reset link/email/code, email me, magic link. These make the server send
   mail, so `POST /forgot_password/` is held as `outbound_message` (not
   `credential_change`). A "Forgot password?" link that only navigates (GET), or a login
   form whose surrounding text mentions it, is not held.
2b. **Account-editing forms** — form submissions and non-GET requests, by the form's
   **field names** (the crawler passes them; a replayed request's JSON/urlencoded body keys
   count too; cross-role replays are held as well):
   - Django admin bulk action (an `action` select plus `_selected_action`/`index`/row
     checkboxes): `delete` when an option is a delete (`delete_selected`),
     `account_change` on a user list (`/admin/auth/user/`, `/users/`), else not held here.
   - `account_change` — any of `username`, `is_superuser`, `is_staff`, `is_active`,
     `is_admin`, `groups`, `user_permissions`, `permissions`, `role`, `roles` (last segment
     of the name, so `user[roles]` counts).
   - `credential_change` — only password-named fields (`password`, `password1/2`,
     `new_password`, `old_password`, `password_confirmation`, …) and/or `email`. A
     `type=password` input with another name (a PIN, VTM's `ssn`) does not count.
   - Not held here: login and registration forms (label or URL says log in / sign in /
     register / sign up / create account / join) unless they carry a privilege field;
     GET forms (search/filter); a plain profile form (`display_name`, `bio`).
3. Keyword categories, checked against label, then URL, then context text:

| Category | Keywords (and inflections) |
|---|---|
| `delete` | delete, remove, destroy, purge, erase, trash, archive |
| `credential_change` | change/reset (my/your/the/this) password or email; password/email change/reset |
| `payment` | pay, payment, purchase, checkout, refund, transfer |
| `outbound_message` | send, sent, invite, invitation, notify, notification, email, sms (on a GET link a bare mail noun — "Email address", "Notifications" — only navigates and is not held) |
| `deactivate` | deactivate, disable, revoke, terminate, cancel + subscription/account/order/booking/reservation/plan/membership/appointment/invoice/ticket, close account |

3a. **account_change** — any non-GET write to an account-admin URL (`/admin/auth/user/…`,
   `/users/<id>/edit|update`, `/accounts/<id>`) the keywords did not already classify
   (`/admin/auth/user/5/delete/` stays `delete`).
4. **delete** — the action opens a `confirm()` dialog ("are you sure"-style), or a form carries
   `data-confirm` / `data-turbo-confirm` / an `onsubmit` confirm.
5. Cross-role replay of POST/PUT/PATCH — **not held** (it measures access control), unless
   step 2b already held it as an account change.
6. **data_create** — production profile only: any POST/PUT/PATCH or form submission from the UI
   or replay.

Each held item records its role, the state it was found in, the full action path to reach it,
the control label, the predicted request and the reason (e.g.
`label matches payment keyword 'checkout'`).

A bare "Cancel" button is not held (only "cancel <noun>"). Logout is never executed, even when
approved. An in-page link (`href="#…"`, `javascript:`) has no URL of its own: it is classified
by its label only and predicts no request (before field fix 3 such items predicted a GET of the
page they sat on, e.g. "Skip to main content" held because the page URL said "delete").

`account_change` is the category to scrutinise most: approving it lets the crawler submit
**generated data** into an account form (username, email, staff/superuser flags, groups). On
an admin role that rewrites real accounts — the crawling role's own included. Recommend
`deny` unless the target is a test account this run created.

## Typical false positives (candidates to downgrade)

- Search/filter UI: "Send" on a search box, "Remove filter", "Clear", "Delete" chips that only
  change client-side state (check the predicted request: no request or a GET). Django admin
  `filter_horizontal` widgets ("Remove", "Remove all", "Choose all") are client-side, but a
  later "Save" of that form would write the change (that Save is held as `account_change` on
  user forms).
- Navigation links whose URL merely contains a keyword (`/help/how-to-delete-your-account`,
  `/docs/payments`) — GETs to documentation pages.
- "Email" or "Notifications" settings **pages** (as opposed to a "Send" action).
- Production `data_create` items that only save harmless preferences (theme, page size) — still
  the human's call, as production data changes.

## Typical false negatives (look for these yourself)

The keyword lists cannot know the domain. Watch for actions that are destructive or have external
side effects without matching a keyword:

- publish / unpublish, approve / reject, submit order, place order, confirm booking
- merge, close (ticket, issue, PR), resolve, finalize, lock, import, bulk update, restore
- regenerate / rotate API key or token, disconnect integration, leave team / organization,
  transfer ownership (matched as `transfer`), change role or permissions
- run job, deploy, trigger webhook, sync with external system, start export to email
- anything that charges, ships, notifies a real person, or calls a third party

If one already ran (non-GET exchanges from the UI), tell the operator at once. If it is still
in the frontier, propose a `deny_patterns` entry; the operator decides.

## Judging risk (for your recommendation)

| Question | Lower risk | Higher risk |
|---|---|---|
| Whose data? | Records this run created (marker `appmap-<run_id>` in URL/label, or ids from this run's writes) | Pre-existing, shared or other users' data |
| Reversible? | Soft delete, archive, toggle | Hard delete, payment, sent message |
| Side effects outside the app? | None | Email/SMS/invite to real addresses, payment provider, webhooks |
| Environment | Staging with seeded data | Production |
| Session impact | None | Logs out, deactivates or changes credentials of the crawling role |

Good recommendations name the deciding factor: "approve — deletes project `appmap-20261024-
120000` that this run created on staging", "deny — sends an invitation email to an address taken
from existing data", "skip — logout, never executed".

## Decisions and their effect

| Decision | Effect |
|---|---|
| `approve` | Executed once by `appmap crawl --approved-only` (then listed as executed). If its target moved (a list shifted, the row is gone) or its predicted request was not observed, it is **not** executed: it goes back to `pending` with `execution: failed: target moved/predicted request not observed`; rules never re-approve it, only the human setting `approve` again retries it |
| `deny` | Never executed; listed as denied, known-but-unexercised surface |
| `skip` | Not executed; treated as not needed (not listed as a gap) |
| `pending` | Waiting for a human; listed as pending in the map |

Revoke an approval that has not run by setting `deny`, never `pending` (a rule could re-approve a
pending item).
