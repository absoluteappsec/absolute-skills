"""Route path normalization into stable patterns."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")
_PARAM = re.compile(r"^(?::(\w+)|\[(\w+)\]|\{(\w+)\})$")


def _param_name(seg: str) -> str | None:
    m = _PARAM.match(seg)
    return next(g for g in m.groups() if g) if m else None


def _match_known(segs: list[str], pattern: str) -> str | None:
    psegs = pattern.strip("/").split("/")
    if len(psegs) != len(segs):
        return None
    out = []
    for seg, pseg in zip(segs, psegs):
        name = _param_name(pseg)
        if name:
            out.append("{%s}" % name)
        elif pseg == seg:
            out.append(seg)
        else:
            return None
    return "/" + "/".join(out)


def normalize_path(path: str, known_patterns: list[str] | None = None,
                   *, marker: str | None = None) -> str:
    path = urlsplit(path).path or "/"
    segs = [s for s in path.strip("/").split("/") if s]
    for pattern in known_patterns or []:
        hit = _match_known(segs, pattern)
        if hit:
            return hit
    out = []
    for s in segs:
        if s.isdigit():
            out.append("{id}")
        elif _UUID.match(s):
            out.append("{uuid}")
        elif _HEX.match(s):
            out.append("{hash}")
        elif marker and marker in s:
            out.append("{slug}")
        else:
            out.append(s)
    return "/" + "/".join(out)
