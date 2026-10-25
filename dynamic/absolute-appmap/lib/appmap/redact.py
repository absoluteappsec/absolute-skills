"""Secret redaction. Credentials and session tokens only; customer data is kept."""
from __future__ import annotations

import json as _json
import re
from contextlib import contextmanager
from urllib.parse import quote, quote_plus, unquote, urlsplit

_JWT = re.compile(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*")
_BEARER = re.compile(r"\bBearer\s+\S+", re.I)
_URL_USERINFO = re.compile(r"(://[^/\s:@]+:)[^/\s@]+(@)")

# (pattern on key, placeholder) in priority order.
_KEY_RULES = [
    (re.compile(r"csrf|xsrf", re.I), "csrf"),
    (re.compile(r"api[-_]?key", re.I), "api_key"),
    (re.compile(r"pass|pwd", re.I), "password"),
    (re.compile(r"secret|token", re.I), "secret"),
]
# HTML carrying tokens outside headers/JSON: <input type=hidden name=authenticity_token
# value=...>, <meta name="csrf-token" content="...">, password inputs with a value.
_HTML_TAG = re.compile(r"<(?:input|meta)\b[^>]*>", re.I)
_HTML_NAME = re.compile(
    r"""\b(?:name|id|property|http-equiv)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
_HTML_TYPE_PW = re.compile(r"""\btype\s*=\s*["']?password\b""", re.I)
_HTML_SECRET_NAME = re.compile(r"csrf|xsrf|authenticity|token|secret|api[-_]?key|pass", re.I)
_HTML_VALUE = re.compile(r"""(\b(?:value|content)\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'>]+)""",
                         re.I)
MIN_LEARNED_LEN = 8  # shorter runtime values (flags, locales) are too common to scrub
MIN_SECRET_LEN = 4  # values the crawler typed into password inputs: scrubbed from 4 chars
_HEADER_CONTAINER_KEYS = {"headers", "request_headers", "response_headers", "req_headers",
                          "resp_headers"}


def _ph(kind: str) -> str:
    return f"<redacted:{kind}>"


def _variants(s: str) -> set[str]:
    return {s, quote(s, safe=""), quote(s), quote_plus(s),
            _json.dumps(s)[1:-1], _json.dumps(s, ensure_ascii=False)[1:-1]}


def _html_tag(m: re.Match) -> str:
    tag = m.group(0)
    names = [next(g for g in n.groups() if g is not None) for n in _HTML_NAME.finditer(tag)]
    hit = next((n for n in names if _HTML_SECRET_NAME.search(n)), None)
    if hit is None and not _HTML_TYPE_PW.search(tag):
        return tag
    kind = "csrf" if hit and re.search(r"csrf|xsrf|authenticity", hit, re.I) else (
        "password" if hit is None or re.search(r"pass", hit, re.I) else "secret")
    return _HTML_VALUE.sub(lambda v: f'{v.group(1)}"{_ph(kind)}"', tag)


class Redactor:
    def __init__(self, secret_values: list[str], extra_patterns: list[str]):
        self._variants: set[str] = set()
        for s in secret_values:
            if s:
                self._variants |= _variants(s)
        self._patterns = [f"(?:{p})" for p in extra_patterns]
        # (host, path) of a form action -> field names that were <input type=password>
        # there; while ``scoped`` to that endpoint they redact like a password key.
        self._pw_fields: dict[tuple[str, str], set[str]] = {}
        self._scope: tuple[str, str] | None = None
        self._compile()

    def _compile(self) -> None:
        # Longest first so a secret containing another is scrubbed whole.
        alts = [re.escape(v) for v in sorted(self._variants, key=len, reverse=True) if v]
        alts += self._patterns
        # One pass: placeholders are never rescanned, so short secrets can't corrupt them.
        self._scrub = re.compile("|".join(alts)) if alts else None

    def learn(self, value: str | None) -> bool:
        """Scrub ``value`` verbatim from now on: session cookie values, Authorization and
        CSRF header values seen at runtime (opaque tokens no key rule can recognise).
        Values shorter than ``MIN_LEARNED_LEN``, plain words (no digit, under 20 chars:
        ``undefined``, ``deleted``) and placeholders are ignored."""
        if not isinstance(value, str):
            return False
        value = value.strip().strip('"')
        if len(value) < MIN_LEARNED_LEN or "<redacted:" in value or value in self._variants \
                or (len(value) < 20 and not any(ch.isdigit() for ch in value)):
            return False
        self._variants |= _variants(value)
        self._compile()
        return True

    def learn_secret(self, value: str | None) -> bool:
        """Scrub ``value`` verbatim from now on: a value the crawler typed into an
        ``<input type=password>`` (whatever the field is named). Unlike ``learn`` there
        is no plain-word filter; values shorter than ``MIN_SECRET_LEN``, and digit-only
        values shorter than ``MIN_LEARNED_LEN`` (ids, prices, years), are ignored (too
        common to scrub from every body; by-name redaction still covers them)."""
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN \
                or "<redacted:" in value \
                or (value.isdigit() and len(value) < MIN_LEARNED_LEN):
            return False
        if value not in self._variants:
            self._variants |= _variants(value)
            self._compile()
        return True

    @staticmethod
    def _endpoint(url: str) -> tuple[str, str]:
        p = urlsplit(url)
        return p.netloc.lower(), p.path.rstrip("/") or "/"

    def learn_password_field(self, url: str, name: str) -> None:
        """Field ``name`` of the form submitting to ``url`` is a password input: request
        bodies/params of that endpoint redact it like a password key (see ``scoped``)."""
        if name:
            self._pw_fields.setdefault(self._endpoint(url), set()).add(name)

    def password_fields(self) -> dict[str, list[str]]:
        """``{"host/path": [names]}`` for persistence across invocations."""
        return {f"{h}{p}": sorted(n) for (h, p), n in sorted(self._pw_fields.items())}

    def load_password_fields(self, saved: dict) -> None:
        for key, names in (saved or {}).items():
            host, _, path = str(key).partition("/")
            for n in names or []:
                self._pw_fields.setdefault((host, "/" + path if path else "/"),
                                           set()).add(str(n))

    @contextmanager
    def scoped(self, url: str | None):
        """Redact as for a request to ``url``: password-type field names learned for
        that endpoint count as password keys. Not re-entrant across awaits: use only
        around synchronous redaction."""
        prev = self._scope
        self._scope = self._endpoint(url) if url else None
        try:
            yield self
        finally:
            self._scope = prev

    def learn_cookies(self, header: str, *, set_cookie: bool = False) -> None:
        """Learn every cookie value of a ``Cookie`` or (newline-joined) ``Set-Cookie``."""
        parts = [line.partition(";")[0] for line in header.split("\n")] if set_cookie \
            else header.split(";")
        for part in parts:
            _name, eq, value = part.strip().partition("=")
            if eq:
                self.learn(value)

    # -- helpers -----------------------------------------------------------
    def _key_kind(self, key: str) -> str | None:
        k = key.lower()
        if k in ("authorization", "proxy-authorization"):
            return "authorization"
        if k in ("cookie", "set-cookie"):
            return "cookie"
        if self._scope is not None and key in self._pw_fields.get(self._scope, ()):
            return "password"
        for pat, kind in _KEY_RULES:
            if pat.search(key):
                return kind
        return None

    def _cookie(self, value: str, *, set_cookie: bool) -> str:
        if set_cookie:
            lines = []
            for line in value.split("\n"):
                first, sep, rest = line.partition(";")
                name, eq, _ = first.partition("=")
                lines.append((f"{name}={_ph('cookie')}" if eq else first) + sep + rest)
            return "\n".join(lines)
        parts = []
        for part in value.split(";"):
            lead = part[: len(part) - len(part.lstrip())]
            name, eq, _ = part.strip().partition("=")
            parts.append(f"{lead}{name}={_ph('cookie')}" if eq else part)
        return ";".join(parts)

    # -- public API --------------------------------------------------------
    def key_kind(self, key: str) -> str | None:
        """Placeholder kind for a field/parameter/header named ``key``, or None to keep it."""
        return self._key_kind(key)

    def literals(self, s: str) -> str:
        """Scrub only literal secrets (configured, learned, ``redact_patterns``)."""
        return self._scrub.sub(_ph("secret"), s) if self._scrub and isinstance(s, str) else s

    def text(self, s: str) -> str:
        if not isinstance(s, str):
            return s
        # Literal secrets first; built-in placeholders are inserted afterwards.
        if self._scrub:
            s = self._scrub.sub(_ph("secret"), s)
        if "<" in s:
            s = _HTML_TAG.sub(_html_tag, s)
        s = _BEARER.sub(_ph("authorization"), s)
        s = _JWT.sub(_ph("jwt"), s)
        return _URL_USERINFO.sub(lambda m: m.group(1) + _ph("password") + m.group(2), s)

    def headers(self, h: dict[str, str]) -> dict:
        out = {}
        for name, value in h.items():
            kind = self._key_kind(name)
            if kind == "cookie":
                out[name] = self._cookie(str(value), set_cookie=name.lower() == "set-cookie")
            elif kind:
                out[name] = _ph(kind)
            else:
                out[name] = self.text(value)
        return out

    def url(self, u: str) -> str:
        base, qmark, rest = u.partition("?")
        base = self.text(base)
        if not qmark:
            return base
        query, hash_, frag = rest.partition("#")
        pairs = []
        for item in query.split("&"):
            k, eq, v = item.partition("=")
            kind = self._key_kind(unquote(k)) if eq else None
            pairs.append(f"{k}={_ph(kind)}" if kind else self.text(item))
        return f"{base}?{'&'.join(pairs)}" + (hash_ + self.text(frag) if hash_ else "")

    def _name_value(self, obj: dict, in_cookies: bool):
        """HAR/Playwright {"name": .., "value": ..} entries: redact value by name."""
        name, value = obj["name"], obj["value"]
        if in_cookies:
            red = _ph("cookie")
        else:
            kind = self._key_kind(name)
            if kind == "cookie":
                red = self._cookie(value, set_cookie=name.lower() == "set-cookie")
            elif kind:
                red = _ph(kind)
            else:
                red = self.text(value)
        return {**{k: self.json(v) for k, v in obj.items() if k != "value"}, "value": red}

    def json(self, obj, _cookies: bool = False):
        if isinstance(obj, dict):
            if isinstance(obj.get("name"), str) and isinstance(obj.get("value"), str):
                return self._name_value(obj, _cookies)
            out = {}
            for k, v in obj.items():
                kind = self._key_kind(k) if isinstance(k, str) else None
                if isinstance(k, str) and k.lower() in _HEADER_CONTAINER_KEYS \
                        and isinstance(v, dict):
                    out[k] = self.headers(v)
                elif isinstance(k, str) and k.lower() == "cookies" and isinstance(v, list):
                    out[k] = self.json(v, True)
                elif kind and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    out[k] = _ph(kind)
                else:
                    out[k] = self.json(v)
            return out
        if isinstance(obj, list):
            return [self.json(v, _cookies) for v in obj]
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, (bytes, bytearray)):
            return self.text(bytes(obj).decode("utf-8", errors="replace"))
        return self.text(str(obj))

    def form(self, pairs: dict) -> dict:
        return self.json(pairs)
