"""``profile-names.yaml``: agent-written purpose and feature names, stable across re-runs.

``technology_overrides`` lets the agent lower a detected technology's confidence with a
reason; upgrades are refused with a warning (never more confident than the evidence).

Features are keyed by their stable key. A name whose key no longer exists moves to ``stale``
(never silently dropped) and returns if the key comes back. Each stub stores the
``suggestion`` it was generated with, so an untouched stub that vanishes is dropped rather
than reported as stale.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

_FIELDS = ("name", "description", "how_it_works")
CONFIDENCE_RANK = {"guessed": 0, "likely": 1, "confirmed": 2}


class NamesFileError(ValueError):
    """The names file exists but is not a YAML mapping."""


def _load(path: Path) -> dict:
    """``{}`` when the file is absent; raises NamesFileError when present but unusable."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        doc = yaml.safe_load(p.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise NamesFileError(f"{p.name}: {type(e).__name__}") from e
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise NamesFileError(f"{p.name}: top level is not a mapping")
    return doc


def _entries(raw, *, coerce: bool = False) -> tuple[dict[str, dict], list]:
    """Well-formed entries by key plus leftovers. Strict (apply): text fields must be
    strings. ``coerce`` (emit): scalars become strings; unusable items are leftovers."""
    out: dict[str, dict] = {}
    left: list = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) \
                or not item["key"]:
            left.append(item)
            continue
        vals = {}
        ok = True
        for f in (*_FIELDS, "suggestion"):
            v = item.get(f)
            if v is None:
                v = ""
            elif not isinstance(v, str):
                if coerce and isinstance(v, (int, float, bool)):
                    v = str(v)
                else:
                    ok = False
                    break
            vals[f] = v
        if not ok:
            left.append(item)
            continue
        entry = {"key": item["key"], **{f: vals[f] for f in _FIELDS}}
        if "suggestion" in item:
            entry["suggestion"] = vals["suggestion"]
        out[item["key"]] = entry
    return out, left


def _untouched(entry: dict) -> bool:
    return (not entry["description"] and not entry["how_it_works"]
            and entry.get("suggestion") is not None and entry["name"] == entry["suggestion"]) \
        or not any(entry[f] for f in _FIELDS)


def emit_profile_names(profile: dict, path: Path) -> list[str]:
    """Write/refresh the names file; returns warnings. An existing file that cannot be
    parsed is left untouched and copied to ``<name>.bak``."""
    path = Path(path)
    try:
        doc = _load(path)
    except NamesFileError as e:
        bak = path.with_name(path.name + ".bak")
        shutil.copyfile(path, bak)
        return [f"{e}; left untouched and copied to {bak.name}. Fix the YAML and re-run "
                "'appmap profile' to refresh it"]
    known, known_left = _entries(doc.get("features"), coerce=True)
    stale, stale_left = _entries(doc.get("stale"), coerce=True)
    suggestions = {f["key"]: f.get("name_suggestion") or ""
                   for f in profile.get("features") or []}

    features = []
    for key in suggestions:
        live, old = known.pop(key, None), stale.pop(key, None)
        entry = live or old
        features.append(entry or {"key": key, "name": suggestions[key], "description": "",
                                  "how_it_works": "", "suggestion": suggestions[key]})
    for key, entry in known.items():  # vanished keys
        if not _untouched(entry):
            stale[key] = entry
    purpose = doc.get("purpose") if isinstance(doc.get("purpose"), str) else ""
    overrides = doc.get("technology_overrides")
    out = {"purpose": purpose, "features": features + known_left,
           "stale": list(stale.values()) + stale_left,
           "technology_overrides": overrides if isinstance(overrides, list) else []}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return []


def apply_profile_names(profile: dict, path: Path) -> int:
    """Fill ``purpose`` and ``feature_names`` from the file; returns the number of feature
    entries applied. Malformed entries, keys not in the profile and an unparseable file
    are ignored."""
    try:
        doc = _load(path)
    except NamesFileError:
        return 0
    if isinstance(doc.get("purpose"), str) and doc["purpose"].strip():
        profile["purpose"] = doc["purpose"].strip()
    valid = {f["key"] for f in profile.get("features") or []}
    names = profile.setdefault("feature_names", {})
    count = 0
    for key, entry in _entries(doc.get("features"))[0].items():
        if key in valid and any(entry[f] for f in _FIELDS):
            names[key] = {f: entry[f] for f in _FIELDS}
            count += 1
    return count


def is_judged(entry: dict | None, suggestion: str) -> bool:
    """A feature counts as judged once it has a name other than the generated suggestion,
    or a description (a pre-filled suggestion alone is not a judgment)."""
    if not entry:
        return False
    name = entry.get("name") or ""
    return bool((name and name != suggestion) or entry.get("description"))


def apply_technology_overrides(profile: dict, path: Path) -> list[str]:
    """Apply ``technology_overrides`` (downgrades only) to ``profile["technologies"]``.

    A downgraded entry keeps the detector's level as ``detected_confidence`` and gets
    ``confidence_reason``. Returns warnings for upgrades, unknown technologies, bad levels
    and malformed entries; those entries change nothing."""
    try:
        doc = _load(path)
    except NamesFileError:
        return []
    raw = doc.get("technology_overrides")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [f"{Path(path).name}: technology_overrides is not a list; ignored"]
    warnings = []
    techs = profile.get("technologies") or []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            warnings.append(f"technology override {item!r}: needs a name; ignored")
            continue
        name, cat = item["name"], item.get("category")
        level, reason = item.get("confidence"), item.get("reason")
        where = f"technology override for {name!r}"
        if level not in CONFIDENCE_RANK:
            warnings.append(f"{where}: confidence {level!r} is not one of "
                            f"{', '.join(CONFIDENCE_RANK)}; ignored")
            continue
        if not isinstance(reason, str) or not reason.strip():
            warnings.append(f"{where}: a reason is required; ignored")
            continue
        matches = [t for t in techs if t.get("name") == name
                   and (not cat or t.get("category") == cat)]
        if not matches:
            warnings.append(f"{where}: no such detected technology; ignored")
            continue
        for t in matches:
            detected = t.get("detected_confidence") or t["confidence"]
            if CONFIDENCE_RANK[level] > CONFIDENCE_RANK.get(detected, 0):
                warnings.append(f"{where}: {level} would upgrade the detected {detected}; "
                                "only downgrades are applied")
            elif level != detected:
                t["detected_confidence"] = detected
                t["confidence"] = level
                t["confidence_reason"] = reason.strip()
    return warnings
