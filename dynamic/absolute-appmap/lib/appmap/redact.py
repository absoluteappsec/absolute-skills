"""Secret redaction. Credentials and session tokens only; customer data is kept."""
from __future__ import annotations

import json as _json
import re
from urllib.parse import quote, quote_plus, unquote

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
        variants: set[str] = set()
        for s in secret_values:
            if s:
                variants |= {s, quote(s, safe=""), quote(s), quote_plus(s),
                             _json.dumps(s)[1:-1], _json.dumps(s, ensure_ascii=False)[1:-1]}
        alts = [re.escape(v) for v in sorted(variants, key=len, reverse=True) if v]
        alts += [f"(?:{p})" for p in extra_patterns]
        # One pass: placeholders are never rescanned, so short secrets can't corrupt them.
        self._scrub = re.compile("|".join(alts)) if alts else None

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
        # Literal secrets first; built-in placeholders are inserted afterwards.
        if self._scrub:
            s = self._scrub.sub(_ph("secret"), s)
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
