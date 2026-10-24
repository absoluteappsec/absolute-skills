"""Secret redaction. Credentials and session tokens only; customer data is kept."""
from __future__ import annotations

import re
from urllib.parse import unquote

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
_HEADER_CONTAINER_KEYS = {"headers", "request_headers", "response_headers", "req_headers",
                          "resp_headers"}


def _ph(kind: str) -> str:
    return f"<redacted:{kind}>"


class Redactor:
    def __init__(self, secret_values: list[str], extra_patterns: list[str]):
        # Longest first so a secret containing another is scrubbed whole.
        self._secrets = sorted({s for s in secret_values if s}, key=len, reverse=True)
        self._extra = [re.compile(p) for p in extra_patterns]

    # -- helpers -----------------------------------------------------------
    def _key_kind(self, key: str) -> str | None:
        k = key.lower()
        if k in ("authorization", "proxy-authorization"):
            return "authorization"
        if k in ("cookie", "set-cookie"):
            return "cookie"
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
    def text(self, s: str) -> str:
        if not isinstance(s, str):
            return s
        s = _BEARER.sub(_ph("authorization"), s)
        s = _JWT.sub(_ph("jwt"), s)
        s = _URL_USERINFO.sub(lambda m: m.group(1) + _ph("password") + m.group(2), s)
        for secret in self._secrets:
            s = s.replace(secret, _ph("secret"))
        for pat in self._extra:
            s = pat.sub(_ph("secret"), s)
        return s

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

    def json(self, obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kind = self._key_kind(k) if isinstance(k, str) else None
                if isinstance(k, str) and k.lower() in _HEADER_CONTAINER_KEYS \
                        and isinstance(v, dict):
                    out[k] = self.headers(v)
                elif kind and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    out[k] = _ph(kind)
                else:
                    out[k] = self.json(v)
            return out
        if isinstance(obj, list):
            return [self.json(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def form(self, pairs: dict) -> dict:
        return self.json(pairs)
