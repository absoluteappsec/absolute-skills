"""Destructive-action classifier: decides whether an action must be held for approval."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

CATEGORIES = ("delete", "credential_change", "account_change", "payment", "outbound_message",
              "deactivate", "logout", "data_create")

# Text is tokenized (camelCase split, non-alphanumerics -> spaces, lowercased) and the
# patterns below are matched against the space-joined tokens, so inflected/camelCase
# forms match ("deleteUser", "Deleted", "/api/sendEmail") but "payload" does not match "pay".
_NOUN = r"(?:subscription|account|order|booking|reservation|plan|membership|appointment|invoice|ticket)s?"
_DET = r"(?:(?:my|your|the|this)\s+)?"
KEYWORDS: list[tuple[str, list[str]]] = [
    ("delete", [r"delet(?:e|es|ed|ing|ion)", r"remov(?:e|es|ed|ing|al)", r"destroy(?:s|ed|ing)?",
                r"purg(?:e|es|ed|ing)", r"eras(?:e|es|ed|ing|ure)", r"trash(?:es|ed|ing)?",
                r"archiv(?:e|es|ed|ing)"]),
    ("credential_change", [r"(?:change|changes|changed|changing|reset|resets|resetting)\s+" + _DET + r"(?:password|email)",
                           r"(?:password|email)\s+(?:change|reset)"]),
    ("payment", [r"pay(?:s|ed|ing|ment|ments)?", r"purchas(?:e|es|ed|ing)", r"check\s*out",
                 r"refund(?:s|ed|ing)?", r"transfer(?:s|red|ring)?"]),
    ("outbound_message", [r"send(?:s|ing)?", r"sent", r"invit(?:e|es|ed|ing|ation|ations)",
                          r"notif(?:y|ies|ied|ication|ications)", r"e\s*mail(?:s|ed|ing)?", r"sms"]),
    ("deactivate", [r"deactivat(?:e|es|ed|ing|ion)", r"disabl(?:e|es|ed|ing)", r"revok(?:e|es|ed|ing)",
                    r"cancel(?:s|led|ling|ed)?\s+" + _DET + _NOUN, r"terminat(?:e|es|ed|ing|ion)",
                    r"clos(?:e|es|ed|ing)\s+" + _DET + r"account"]),
]
_COMPILED = [(cat, re.compile(r"\b(?:" + "|".join(ws) + r")\b")) for cat, ws in KEYWORDS]
# Account-recovery / verification requests that make the server send mail. Checked before
# KEYWORDS (a "password reset request" is outbound mail, not a credential change), but
# only on label/URL of submits and non-GET requests: a "Forgot password?" link that merely
# navigates, or a login form whose context mentions it, is not held.
_RECOVERY = re.compile(r"\b(?:" + "|".join([
    r"forgot(?:ten)?\s+(?:your\s+|my\s+)?(?:password|username|login)",
    r"password\s+reset\s+request",
    r"resend\s+(?:the\s+|your\s+|my\s+|a\s+)?(?:verification|confirmation|activation)"
    r"\s+(?:e\s*mail|link|code)",
    r"send\s+(?:me\s+)?(?:a\s+|the\s+)?(?:password\s+)?reset\s+(?:link|e\s*mail|code)",
    r"e\s*mail\s+me",
    r"magic\s+link",
]) + r")\b")
_LOGOUT = re.compile(r"\b(?:log\s*out|sign\s*out|log\s*off|sign\s*off)\b")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _tok(text: str) -> str:
    return _NON_ALNUM.sub(" ", _CAMEL.sub(r"\1 \2", text).lower()).strip()


_WRITE = {"POST", "PUT", "PATCH"}


@dataclass
class ActionDescriptor:
    kind: Literal["click", "submit", "request"]
    method: str | None
    url: str | None
    label: str
    context_text: str
    has_confirm: bool
    role: str
    origin: str
    # Form submits: the form's controls as {"name", "type"[, "options"]} dicts (field fix
    # 3: account-editing forms are recognised by their field names). None = unknown.
    fields: list[dict] | None = None


@dataclass
class Flag:
    held: bool
    category: str | None = None
    reasons: list[str] = field(default_factory=list)


def _held(category: str, reason: str) -> Flag:
    return Flag(True, category, [reason])


# -- account-editing forms (field fix 3) ------------------------------------------------
# Field names (last bracket/dot segment, lowercased) that put a form on an account:
# privilege/identity fields -> account_change; password/email-only -> credential_change.
_PRIVILEGE_FIELDS = {"is_superuser", "is_staff", "is_active", "is_admin", "groups",
                     "user_permissions", "permissions", "role", "roles", "username"}
_CREDENTIAL_FIELD = re.compile(r"^(?:(?:new|old|current|confirm)_?)?(?:password|passwd)"
                               r"(?:_?(?:\d|confirm(?:ation)?|again|repeat))?$|^email$")
_NAME_SEG = re.compile(r"[\[\].]+")
_LOGIN_OR_SIGNUP = re.compile(r"\b(?:log\s*in|sign\s*in|regist(?:er|ers|ered|ering|ration)"
                              r"|sign\s*up|create\s+(?:an\s+|your\s+|new\s+)?account|join)\b")
_ACCOUNT_URL = re.compile(r"/admin/auth/user(?:/|$)|/users?/\d+/(?:edit|update)(?:/|$)"
                          r"|/accounts?/\d+(?:/|$)", re.I)
_USER_LIST_URL = re.compile(r"/admin/auth/user/?$|/users?/?$", re.I)
_DELETE_WORD = re.compile(r"delet|remov|destroy|purg|eras|trash")


def _field_key(name: str) -> str:
    parts = [p for p in _NAME_SEG.split(str(name or "").lower()) if p]
    return parts[-1] if parts else ""


def body_fields(text: str) -> list[dict] | None:
    """Top-level keys of a JSON object or urlencoded body (replayed requests)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        return [{"name": k, "type": "text"} for k in obj] if isinstance(obj, dict) else None
    if "=" in text and " " not in text.split("&", 1)[0]:
        return [{"name": kv.split("=", 1)[0], "type": "text"} for kv in text.split("&") if kv]
    return None


def _account_flag(a: ActionDescriptor, fields: list[dict], tl: str, tu: str) -> Flag | None:
    """A form submit / write that edits an account (field fix 3), or a bulk action."""
    keys = {_field_key(f.get("name")): f for f in fields if isinstance(f, dict)}
    # Django admin changelist bulk action: an ``action`` select plus selected rows
    act = keys.get("action")
    if act is not None and ("_selected_action" in keys or "index" in keys
                            or any(f.get("type") == "checkbox" for f in keys.values())):
        opts = [_tok(str(o)) for o in act.get("options") or []]
        hit = next((o for o in opts if _DELETE_WORD.search(o)), None)
        if hit:
            return _held("delete", f"bulk action form offers '{hit}'")
        if _USER_LIST_URL.search(a.url or ""):
            return _held("account_change", "bulk action form on a user list")
    privilege = sorted(k for k in keys if k in _PRIVILEGE_FIELDS)
    # by name only: a type=password input may hold other secrets (a PIN, an SSN)
    creds = sorted(k for k in keys if _CREDENTIAL_FIELD.match(k))
    if not privilege and not creds:
        return None
    signup = _LOGIN_OR_SIGNUP.search(tl) or _LOGIN_OR_SIGNUP.search(tu)
    if signup and not (set(privilege) - {"username"}):
        return None  # a login or registration form (creates no change to an account)
    if privilege:
        return _held("account_change", f"form has account field(s) {', '.join(privilege)}")
    return _held("credential_change", f"form has credential field(s) {', '.join(creds)}")


def classify(a: ActionDescriptor, profile: str) -> Flag:
    method = (a.method or "").upper()
    label, url, ctx = a.label or "", a.url or "", a.context_text or ""

    tl, tu, tc = _tok(label), _tok(url), _tok(ctx)
    if _LOGOUT.search(tl) or _LOGOUT.search(tu):
        return _held("logout", "logout/sign-out action")
    if method == "DELETE":
        return _held("delete", "HTTP DELETE request")
    if a.kind == "submit" or (method and method != "GET"):
        for source, text in (("label", tl), ("url", tu)):
            m = _RECOVERY.search(text)
            if m:
                return _held("outbound_message",
                             f"{source} matches account-recovery mail keyword '{m.group(0)}'")
    writes = method != "GET" and (a.kind == "submit" or bool(method))
    fields = a.fields
    if fields is None and a.kind == "request" and a.origin != "ui":
        fields = body_fields(ctx)  # replayed request: its body is the context text
    if writes and fields:
        f = _account_flag(a, fields, tl, tu)
        if f is not None:
            return f
    for cat, rx in _COMPILED:
        for source, text in (("label", tl), ("url", tu), ("context", tc)):
            m = rx.search(text)
            if m:
                return _held(cat, f"{source} matches {cat} keyword '{m.group(0).lower()}'")
    if writes and _ACCOUNT_URL.search(url):
        return _held("account_change", "write to an account-admin URL")
    if a.has_confirm:
        return _held("delete", "action opens a confirmation dialog")
    if a.origin == "cross_role" and method in _WRITE:
        return Flag(False)  # spec decision 10
    if a.origin not in ("cross_role", "approved") and profile == "production" and (
            method in _WRITE or (a.kind == "submit" and method in ("", "POST"))):
        return _held("data_create", "state-changing request on production profile")
    return Flag(False)
