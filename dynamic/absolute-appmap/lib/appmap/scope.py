"""Scope guard: decides whether a URL may be requested."""
from __future__ import annotations

from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from .config import Scope


class ScopeGuard:
    def __init__(self, scope: Scope):
        self.scope = scope
        self._hosts = {h.lower() for h in scope.allow_hosts}

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or host not in self._hosts:
            return False
        path = parts.path or "/"
        prefixes = self.scope.allow_path_prefixes
        if prefixes and not any(path.startswith(p) for p in prefixes):
            return False
        return not any(fnmatchcase(path, pat) for pat in self.scope.deny_patterns)
