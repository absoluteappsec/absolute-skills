"""Destructive-action classifier: decides whether an action must be held for approval."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

CATEGORIES = ("delete", "credential_change", "payment", "outbound_message",
              "deactivate", "logout", "data_create")

LOGOUT_WORDS = [r"log\s*out", r"sign\s*out", r"log\s*off", r"sign\s*off"]
LOGOUT_URL = re.compile(r"/(log-?out|sign-?out)(\b|/|$|\?)", re.I)

# Keyword lists, checked in this order after logout/DELETE.
KEYWORDS: list[tuple[str, list[str]]] = [
    ("delete", [r"delete", r"remove", r"destroy", r"purge"]),
    ("credential_change", [r"change\s+(my\s+)?(password|email)", r"reset\s+(my\s+)?(password|email)",
                           r"(password|email)\s+(change|reset)", r"change[-_]?(password|email)",
                           r"reset[-_]?(password|email)"]),
    ("payment", [r"pay", r"payment", r"purchase", r"checkout", r"check\s*out", r"refund", r"transfer"]),
    ("outbound_message", [r"send", r"invite", r"notify", r"email", r"sms"]),
    ("deactivate", [r"deactivate", r"disable", r"revoke", r"cancel\s+(my\s+)?(subscription|account)"]),
]
_COMPILED = [(cat, re.compile(r"(?<![a-z0-9])(?:" + "|".join(ws) + r")(?![a-z0-9])", re.I))
             for cat, ws in KEYWORDS]
_LOGOUT = re.compile(r"(?<![a-z0-9])(?:" + "|".join(LOGOUT_WORDS) + r")(?![a-z0-9])", re.I)
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


@dataclass
class Flag:
    held: bool
    category: str | None = None
    reasons: list[str] = field(default_factory=list)


def _held(category: str, reason: str) -> Flag:
    return Flag(True, category, [reason])


def classify(a: ActionDescriptor, profile: str) -> Flag:
    method = (a.method or "").upper()
    label, url, ctx = a.label or "", a.url or "", a.context_text or ""

    if _LOGOUT.search(label) or _LOGOUT.search(url) or LOGOUT_URL.search(url):
        return _held("logout", "logout/sign-out action")
    if method == "DELETE":
        return _held("delete", "HTTP DELETE request")
    for cat, rx in _COMPILED:
        for source, text in (("label", label), ("url", url), ("context", ctx)):
            m = rx.search(text)
            if m:
                return _held(cat, f"{source} matches {cat} keyword '{m.group(0).lower()}'")
    if a.has_confirm:
        return _held("delete", "action opens a confirmation dialog")
    if a.origin == "cross_role" and method in _WRITE:
        return Flag(False)  # spec decision 10
    if a.origin not in ("cross_role", "approved") and profile == "production" and (
            method in _WRITE or (a.kind == "submit" and method in ("", "POST"))):
        return _held("data_create", "state-changing request on production profile")
    return Flag(False)
