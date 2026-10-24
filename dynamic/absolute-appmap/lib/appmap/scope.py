"""Scope guard: decides whether a URL may be requested."""
from __future__ import annotations

from fnmatch import fnmatchcase
from urllib.parse import unquote, urlsplit

from .config import Scope


def _canonical(path: str) -> str:
    """Percent-decode to a fixed point, drop ;params, resolve dot-segments."""
    for _ in range(10):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.replace("\\", "/")
    out: list[str] = []
    for seg in path.split("/"):
        seg = seg.split(";", 1)[0]
        if seg == "..":
            if out:
                out.pop()
        elif seg not in ("", "."):
            out.append(seg)
    return "/" + "/".join(out)


def _variants(path: str) -> set[str]:
    base = {path.lower(), _canonical(path).lower()}
    out = set()
    for v in base:
        stripped = v.rstrip("/") or "/"
        out.update({v, stripped, stripped.rstrip("/") + "/"})
    return out


class ScopeGuard:
    def __init__(self, scope: Scope):
        self.scope = scope
        self._hosts = {h.lower() for h in scope.allow_hosts}
        self._deny = [d.lower() for d in scope.deny_patterns]

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or host not in self._hosts:
            return False
        raw = parts.path or "/"
        canon = _canonical(raw)
        prefixes = self.scope.allow_path_prefixes
        if prefixes and not any(self._under(canon, p) for p in prefixes):
            return False
        return not any(fnmatchcase(v, pat) for v in _variants(raw) for pat in self._deny)

    @staticmethod
    def _under(path: str, prefix: str) -> bool:
        base = prefix.rstrip("/")
        return base == "" or path == base or path.startswith(base + "/")
