"""Test-data generation for form fields: marker-bearing, constraint-aware, hint-driven retries."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser.forms import FieldInfo

SAMPLES = Path(__file__).parent / "resources" / "samples"
PHONE = "+15555550100"
_PASSWORD_TAIL = "-Aa1!"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# --- simple regex (char-class) support ------------------------------------------------

_ESCAPES = {"d": "0123456789", "w": _ALPHABET + "_", "s": " "}


def _parse_class(p: str, i: int) -> tuple[set[str], int]:
    """Parse ``[...]`` starting at p[i] == '['; return (allowed chars, index after ']')."""
    i += 1
    neg = p[i:i + 1] == "^"
    if neg:
        i += 1
    chars: set[str] = set()
    first = True
    while i < len(p) and (p[i] != "]" or first):
        first = False
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            e = p[i + 1]
            chars |= set(_ESCAPES.get(e, e))
            i += 2
        elif i + 2 < len(p) and p[i + 1] == "-" and p[i + 2] != "]":
            chars |= {chr(o) for o in range(ord(c), ord(p[i + 2]) + 1)}
            i += 3
        else:
            chars.add(c)
            i += 1
    if i >= len(p):
        raise ValueError("unterminated class")
    if neg:
        chars = {c for c in _ALPHABET + "ABCDEFGHIJKLMNOPQRSTUVWXYZ-" if c not in chars}
    return chars, i + 1


def _parse_pattern(p: str) -> list[tuple[set[str], int, int]]:
    """Sequence of (allowed chars, min, max) atoms; raises ValueError for unsupported syntax."""
    atoms: list[tuple[set[str], int, int]] = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "[":
            chars, i = _parse_class(p, i)
        elif c == "\\" and i + 1 < len(p):
            e = p[i + 1]
            chars = set(_ESCAPES.get(e, e))
            i += 2
        elif c == ".":
            chars, i = set(_ALPHABET), i + 1
        elif c in "()|^$*+?{":
            raise ValueError("unsupported pattern syntax")
        else:
            chars, i = {c}, i + 1
        lo, hi = 1, 1
        m = re.match(r"\{(\d+)(?:(,)(\d*))?\}", p[i:])
        if m:
            lo = int(m.group(1))
            hi = (int(m.group(3)) if m.group(3) else lo + 20) if m.group(2) else lo
            i += m.end()
        elif p[i:i + 1] in ("+", "*", "?"):
            lo, hi = {"+": (1, 21), "*": (0, 20), "?": (0, 1)}[p[i]]
            i += 1
        atoms.append((chars, lo, hi))
    return atoms


def _unanchor(p: str) -> str:
    """HTML ``pattern`` is implicitly anchored; drop explicit ^ / $."""
    if p.startswith("^"):
        p = p[1:]
    if p.endswith("$") and not p.endswith("\\$"):
        p = p[:-1]
    return p


def _from_pattern(pattern: str, seed: str) -> str | None:
    """A string fully matching ``pattern`` that reuses as much of ``seed`` as the atoms allow."""
    try:
        atoms = _parse_pattern(_unanchor(pattern))
    except ValueError:
        return None
    out, pos = [], 0
    for chars, lo, hi in atoms:
        n = 0
        while n < hi and pos < len(seed) and seed[pos] in chars:
            out.append(seed[pos])
            pos += 1
            n += 1
        if n < lo:
            fill = sorted(chars)[0] if chars else "a"
            fill = next((c for c in "a0" if c in chars), fill)
            out.append(fill * (lo - n))
    value = "".join(out)
    return value if re.fullmatch(pattern, value) else None


def _fit_pattern(value: str, field: "FieldInfo") -> str:
    if field.pattern:
        try:
            if re.fullmatch(field.pattern, value):
                return value
        except re.error:
            return value
        return _from_pattern(field.pattern, value) or value
    return value


# --- hints ------------------------------------------------------------------------------

_HINT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d+)\s*(?:-|to|and)\s*(\d+)\s*char", re.I), "len:{0}-{1}"),
    (re.compile(r"(?:at most|no more than|more than|maximum(?: length)?(?: of| is)?|max(?:imum)?|up to|under)\s*(\d+)", re.I), "max:{0}"),
    (re.compile(r"(?:at least|minimum(?: length)?(?: of| is)?|min(?:imum)?|no fewer than)\s*(\d+)", re.I), "min:{0}"),
    (re.compile(r"too long|exceeds?|shorten|shorter", re.I), "max:8"),
    (re.compile(r"too short|longer", re.I), "min:12"),
    (re.compile(r"(?:invalid|valid|enter an?)\s+e-?mail|email address", re.I), "email"),
    (re.compile(r"(?:invalid|valid)\s+(?:url|link|website)", re.I), "url"),
    (re.compile(r"(?:invalid|valid)\s+(?:phone|telephone)", re.I), "phone"),
    (re.compile(r"(?:valid|invalid)\s+(?:number|integer)|must be (?:a )?numeric|numbers? only", re.I), "number"),
    (re.compile(r"must match|match the requested|pattern|invalid format|format", re.I), "pattern"),
    (re.compile(r"required|fill out|can'?t be blank|must not be empty|cannot be empty", re.I), "required"),
]


def hint_from_validation(message: str) -> str | None:
    """Map a validation message to a hint token: max:N, min:N, len:A-B, email, url, phone, number, pattern, required."""
    for rx, fmt in _HINT_RULES:
        m = rx.search(message or "")
        if m:
            return fmt.format(*m.groups())
    return None


# --- generator --------------------------------------------------------------------------

_DATE_FORMATS = {
    "date": "%Y-%m-%d", "datetime-local": "%Y-%m-%dT%H:%M", "month": "%Y-%m",
    "time": "%H:%M", "week": "%G-W%V",
}


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def _fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


class DataGen:
    def __init__(self, marker: str, fixtures: dict | None):
        self.marker = marker
        self._rules = [(re.compile(rx, re.I), v)
                       for rx, v in ((fixtures or {}).get("fields") or {}).items()]

    def _fixture(self, field: "FieldInfo"):
        for rx, value in self._rules:
            if rx.search(field.name or "") or (field.label and rx.search(field.label)):
                return value.replace("{marker}", self.marker) if isinstance(value, str) else value
        return None

    def _marker(self, room: int | None) -> str:
        return self.marker if room is None else self.marker[:max(1, room)]

    def _sample(self, accept: list[str]) -> Path:
        acc = " ".join(accept).lower()
        for needle, name in (("image", "sample.png"), (".png", "sample.png"), (".jpg", "sample.png"),
                             (".jpeg", "sample.png"), ("pdf", "sample.pdf"), ("csv", "sample.csv")):
            if needle in acc:
                return SAMPLES / name
        return SAMPLES / "sample.txt"

    def value_for(self, field: "FieldInfo", attempt: int, hint: str | None = None) -> str | list[str] | Path | bool:
        fx = self._fixture(field)
        if fx is not None:
            return fx
        w, t = field.widget, (field.type or "").lower()
        opts = [o for o in field.options if o != ""]
        if w == "file":
            return self._sample(field.options)
        if w == "checkbox":
            return True
        if w == "radio" or w == "select":
            return opts[0] if opts else ""
        if w == "multiselect":
            return opts[:1]
        if w == "combobox":
            return opts[0] if opts else self.marker
        if w == "date" or t in _DATE_FORMATS:
            return datetime.now().strftime(_DATE_FORMATS.get(t, _DATE_FORMATS["date"]))
        ident = f"{field.name} {field.label}".lower()
        if t in ("number", "range"):
            lo, hi = _num(field.min), _num(field.max)
            if lo is not None and hi is not None:
                return _fmt_num((lo + hi) / 2 if t == "range" else float(int((lo + hi) / 2)))
            return _fmt_num(lo if lo is not None else hi if hi is not None else 1)
        if t == "color":
            return "#336699"
        room = field.maxlength
        if hint and hint.startswith("max:"):
            room = min(room or 10**6, int(hint[4:]))
        elif hint and hint.startswith("len:"):
            room = min(room or 10**6, int(hint[4:].split("-")[1]))
        if t == "email" or hint == "email" or re.search(r"e-?mail", ident):
            return f"{self._marker(room and room - 13)}@example.test"
        if t == "tel" or hint == "phone" or re.search(r"phone|mobile|tel\b", ident):
            return PHONE
        if t == "url" or hint == "url" or re.search(r"\burl\b|website", ident):
            return f"https://example.test/{self._marker(room and room - 21)}"
        if t == "password":
            return self._marker(room and room - len(_PASSWORD_TAIL)) + _PASSWORD_TAIL
        value = self._marker(room)
        if hint:
            m = re.fullmatch(r"(?:min:(\d+)|len:(\d+)-\d+)", hint)
            if m:
                need = int(m.group(1) or m.group(2))
                if len(value) < need:
                    value = value + "a" * (need - len(value))
        if field.maxlength:
            value = value[:field.maxlength]
        return _fit_pattern(value, field)
