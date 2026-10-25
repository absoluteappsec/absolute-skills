"""Data-driven technology fingerprinting over profile ``Evidence``.

Rules live in ``resources/fingerprints.yaml``. A rule matches one or more evidence kinds with a
regex; an optional version regex (one capture group, anchored to banner/header/path forms by
the rule author) is applied to the same evidence text. Confidence follows the profile rules:
the rule's own level, ``confirmed`` when two distinct rules agree on a technology, and
``likely`` for technologies only implied by another (e.g. Django -> Python).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Iterable

import yaml

from appmap.profile.evidence import Evidence
from appmap.redact import Redactor

CATEGORIES = ("language", "server_framework", "frontend_framework", "js_library",
              "css_framework", "web_server", "app_server", "cdn_proxy", "auth", "api_style",
              "third_party")
EVIDENCE_KINDS = ("header", "cookie", "html", "script", "source_map", "schema", "spa",
                  "error_page", "auth")
RULE_CONFIDENCE = ("confirmed", "likely")
MAX_EVIDENCE = 3
MAX_SNIPPET = 200
_SNIPPET_LEAD = 40
_redactor = Redactor([], [])


@dataclass(frozen=True)
class Rule:
    tech: str
    category: str
    evidence: tuple[str, ...]
    match: re.Pattern
    version: re.Pattern | None
    confidence: str
    implies: tuple[tuple[str, str], ...] = ()


@dataclass
class Technology:
    name: str
    category: str
    versions: list[str]
    confidence: str
    evidence: list[dict] = field(default_factory=list)


def _compile(pattern, what: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{what}: regex must be a non-empty string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{what}: invalid regex {pattern!r}: {exc}") from None


def _rule(raw: dict, i: int) -> Rule:
    if not isinstance(raw, dict):
        raise ValueError(f"rule {i}: not a mapping")
    tech = raw.get("tech")
    where = f"rule {i} ({tech})"
    if not isinstance(tech, str) or not tech:
        raise ValueError(f"{where}: tech is required")
    if raw.get("category") not in CATEGORIES:
        raise ValueError(f"{where}: unknown category {raw.get('category')!r}")
    kinds = raw.get("evidence")
    if not isinstance(kinds, list) or not kinds or any(k not in EVIDENCE_KINDS for k in kinds):
        raise ValueError(f"{where}: evidence must list kinds from {EVIDENCE_KINDS}")
    if raw.get("confidence") not in RULE_CONFIDENCE:
        raise ValueError(f"{where}: confidence must be one of {RULE_CONFIDENCE}")
    version = None
    if raw.get("version") is not None:
        version = _compile(raw["version"], f"{where} version")
        if version.groups != 1:
            raise ValueError(f"{where}: version regex needs exactly one capture group")
    implies = []
    for imp in raw.get("implies") or []:
        if not (isinstance(imp, dict) and imp.get("tech") and imp.get("category") in CATEGORIES):
            raise ValueError(f"{where}: implies entries need tech and a known category")
        implies.append((imp["tech"], imp["category"]))
    return Rule(tech, raw["category"], tuple(kinds), _compile(raw.get("match"), f"{where} match"),
                version, raw["confidence"], tuple(implies))


def load_rules(path: Path | None = None) -> list[Rule]:
    """Load and validate the rule table (default: the packaged ``fingerprints.yaml``)."""
    if path is None:
        text = resources.files("appmap").joinpath("resources/fingerprints.yaml").read_text()
    else:
        text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    raw_rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(raw_rules, list):
        raise ValueError("fingerprint rules file must have a top-level 'rules' list")
    return [_rule(r, i) for i, r in enumerate(raw_rules)]


def _snippet(text: str, m: re.Match) -> str:
    """Short re-redacted excerpt: the match's lines, starting at most a little before it."""
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line_end = len(text) if line_end < 0 else line_end
    start = max(line_start, m.start() - _SNIPPET_LEAD)
    piece = " ".join(text[start:min(line_end, start + 4 * MAX_SNIPPET)].split())
    return _redactor.text(piece)[:MAX_SNIPPET]


def _version_key(v: str):
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-+]", v))


def _pick_evidence(hits: dict[int, list[dict]]) -> list[dict]:
    """Up to MAX_EVIDENCE entries, one per distinct rule first, then further hits."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for rnd in range(MAX_EVIDENCE):
        for entries in hits.values():
            if rnd < len(entries) and len(out) < MAX_EVIDENCE:
                e = entries[rnd]
                key = (repr(e["ref"]), e["kind"], e["snippet"])
                if key not in seen:
                    seen.add(key)
                    out.append(e)
    return out


def detect(evidence: Iterable[Evidence], rules: list[Rule]) -> list[Technology]:
    """Run ``rules`` over ``evidence`` and return detected technologies, sorted by category."""
    by_kind: dict[str, list[tuple[int, Rule]]] = {}
    for idx, rule in enumerate(rules):
        for kind in rule.evidence:
            by_kind.setdefault(kind, []).append((idx, rule))

    category: dict[str, str] = {}
    versions: dict[str, set[str]] = {}
    hits: dict[str, dict[int, list[dict]]] = {}  # tech -> rule index -> evidence entries
    for ev in evidence:
        for idx, rule in by_kind.get(ev.kind, ()):
            m = rule.match.search(ev.text)
            if not m:
                continue
            category.setdefault(rule.tech, rule.category)
            found = versions.setdefault(rule.tech, set())
            vms = list(rule.version.finditer(ev.text)) if rule.version is not None else []
            found.update(vm.group(1) for vm in vms)
            shown = vms[0] if vms else m  # prefer showing the versioned form
            entries = hits.setdefault(rule.tech, {}).setdefault(idx, [])
            if len(entries) < MAX_EVIDENCE:
                entries.append({"ref": ev.ref, "kind": ev.kind,
                                "snippet": _snippet(ev.text, shown)})

    techs: dict[str, Technology] = {}
    for tech, by_rule in hits.items():
        confirmed = len(by_rule) >= 2 or any(rules[i].confidence == "confirmed" for i in by_rule)
        techs[tech] = Technology(tech, category[tech], sorted(versions[tech], key=_version_key),
                                 "confirmed" if confirmed else "likely", _pick_evidence(by_rule))

    implied: dict[str, tuple[str, dict[int, list[dict]]]] = {}
    for tech, by_rule in hits.items():
        for idx, entries in by_rule.items():
            for name, cat in rules[idx].implies:
                if name not in techs:
                    implied.setdefault(name, (cat, {}))[1].setdefault(idx, entries)
    for name, (cat, by_rule) in implied.items():
        techs[name] = Technology(name, cat, [], "likely", _pick_evidence(by_rule))

    return sorted(techs.values(), key=lambda t: (CATEGORIES.index(t.category), t.name.lower()))
