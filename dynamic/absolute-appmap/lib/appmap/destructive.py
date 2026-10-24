"""Destructive-action classifier: decides whether an action must be held for approval."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

CATEGORIES = ("delete", "credential_change", "payment", "outbound_message",
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

    tl, tu, tc = _tok(label), _tok(url), _tok(ctx)
    if _LOGOUT.search(tl) or _LOGOUT.search(tu):
        return _held("logout", "logout/sign-out action")
    if method == "DELETE":
        return _held("delete", "HTTP DELETE request")
    for cat, rx in _COMPILED:
        for source, text in (("label", tl), ("url", tu), ("context", tc)):
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
