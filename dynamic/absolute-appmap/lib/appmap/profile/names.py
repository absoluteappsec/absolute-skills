"""``profile-names.yaml``: agent-written purpose and feature names, stable across re-runs.

Features are keyed by their stable key. A name whose key no longer exists moves to ``stale``
(never silently dropped) and returns if the key comes back.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_FIELDS = ("name", "description", "how_it_works")


def _load(path: Path) -> dict:
    try:
        doc = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _entries(raw) -> dict[str, dict]:
    """Well-formed entries by key: dicts with a string key; text fields must be strings."""
    out: dict[str, dict] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            continue
        if any(not isinstance(item.get(f, ""), str) for f in _FIELDS):
            continue
        out[item["key"]] = {"key": item["key"], **{f: item.get(f) or "" for f in _FIELDS}}
    return out


def emit_profile_names(profile: dict, path: Path) -> None:
    doc = _load(path)
    known = _entries(doc.get("features"))
    stale = _entries(doc.get("stale"))
    current = [f["key"] for f in profile.get("features") or []]
    suggestions = {f["key"]: f.get("name_suggestion") or "" for f in profile.get("features") or []}

    features = []
    for key in current:
        entry = known.pop(key, None) or stale.pop(key, None)
        features.append(entry or {"key": key, "name": suggestions[key],
                                  "description": "", "how_it_works": ""})
    stale.update(known)  # vanished keys
    purpose = doc.get("purpose") if isinstance(doc.get("purpose"), str) else ""
    out = {"purpose": purpose, "features": features, "stale": list(stale.values())}
    Path(path).write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))


def apply_profile_names(profile: dict, path: Path) -> int:
    """Fill ``purpose`` and ``feature_names`` from the file; returns the number of feature
    entries applied. Malformed entries and keys not in the profile are ignored."""
    doc = _load(path)
    if isinstance(doc.get("purpose"), str) and doc["purpose"].strip():
        profile["purpose"] = doc["purpose"].strip()
    valid = {f["key"] for f in profile.get("features") or []}
    names = profile.setdefault("feature_names", {})
    count = 0
    for key, entry in _entries(doc.get("features")).items():
        if key in valid and any(entry[f] for f in _FIELDS):
            names[key] = {f: entry[f] for f in _FIELDS}
            count += 1
    return count
