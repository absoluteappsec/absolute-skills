"""Run configuration: YAML loading, profile defaults and validation."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

PROFILES = ("staging", "production")
LOGIN_TYPES = ("form", "manual", "none", "storage_state")
SPA_MODES = ("auto", "on", "off")

_PROFILE_LIMITS = {
    "staging": {"max_requests_per_second": 5.0, "jitter_seconds": 0.0,
                "concurrency": 4, "breaker_threshold": 0.5},
    "production": {"max_requests_per_second": 1.0, "jitter_seconds": 0.5,
                   "concurrency": 1, "breaker_threshold": 0.2},
}
_COMMON_LIMITS = {
    "breaker_window": 20, "max_states_per_route_pattern": 5,
    "max_runtime_minutes": 240, "max_depth": 15,
    "quiet_window_ms": 500, "max_form_retries": 3,
}


TOP_KEYS = ("target", "base_urls", "scope", "authorization", "profile", "limits",
            "roles", "discovery", "form_data", "spa", "redact_patterns")


class ConfigError(ValueError):
    """Raised for any invalid or unsafe run configuration."""


@dataclass
class Scope:
    allow_hosts: list[str]
    allow_path_prefixes: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


@dataclass
class LoginSpec:
    type: Literal["form", "manual", "none", "storage_state"]
    url: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    storage_state_path: str | None = None
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    success_selector: str | None = None


@dataclass
class Role:
    name: str
    login: LoginSpec


@dataclass
class Authorization:
    confirmed_by: str
    notes: str = ""


@dataclass
class Limits:
    max_requests_per_second: float
    jitter_seconds: float
    concurrency: int
    max_states_per_route_pattern: int
    max_runtime_minutes: int
    max_depth: int
    quiet_window_ms: int
    max_form_retries: int
    breaker_window: int
    breaker_threshold: float


@dataclass
class Discovery:
    passive_sources: bool = True
    schemas: bool = True
    bundle_mining: bool = True
    api_replay: bool = True
    forced_browsing: bool = False
    wordlists: list[str] = field(default_factory=list)


@dataclass
class FormData:
    marker_prefix: str = "appmap-{run_id}"
    fixtures: str | None = None


@dataclass
class RunConfig:
    base_urls: list[str]
    scope: Scope
    authorization: Authorization
    profile: Literal["staging", "production"]
    limits: Limits
    roles: list[Role]
    discovery: Discovery
    form_data: FormData
    spa: Literal["auto", "on", "off"]
    redact_patterns: list[str]
    source_path: Path

    def credentials(self, role: str) -> tuple[str, str] | None:
        """Read (username, password) from the role's env vars, at call time."""
        for r in self.roles:
            if r.name == role:
                u, p = r.login.username_env, r.login.password_env
                if not u or not p:
                    return None
                user, pw = os.environ.get(u), os.environ.get(p)
                return (user, pw) if user is not None and pw is not None else None
        return None

    def to_redacted_dict(self) -> dict:
        """Plain-data view holding env var NAMES only, never their values."""
        d = asdict(self)
        d["source_path"] = str(self.source_path)
        return d


def _check_keys(raw: dict, allowed, what: str) -> None:
    unknown = set(raw) - set(allowed)
    if unknown:
        raise ConfigError(f"{what}: unknown keys {sorted(unknown)}")


def _num(values: dict, key: str, *, integer: bool, lo: float,
         lo_open: bool = False, hi: float | None = None) -> None:
    v = values[key]
    ok_type = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok_type:
        raise ConfigError(
            f"limits.{key} must be {'an integer' if integer else 'a number'}")
    if (v <= lo if lo_open else v < lo) or (hi is not None and v > hi):
        rng = f"{'>' if lo_open else '>='} {lo}" + (f" and <= {hi}" if hi is not None else "")
        raise ConfigError(f"limits.{key} must be {rng}")


def _mapping(raw, what: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{what} must be a mapping")
    return raw


def _strs(raw, what: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{what} must be a list of strings")
    return list(raw)


def _bool(raw: dict, key: str, default: bool, what: str) -> bool:
    v = raw.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{what}.{key} must be true or false")
    return v


def _parse_login(raw, role: str) -> LoginSpec:
    raw = _mapping(raw, f"roles[{role}].login")
    if "password" in raw or "username" in raw:
        raise ConfigError(
            f"roles[{role}].login: credentials must come from env "
            "(use username_env / password_env)")
    ltype = raw.get("type", "none")
    if ltype not in LOGIN_TYPES:
        raise ConfigError(
            f"roles[{role}].login.type must be one of {', '.join(LOGIN_TYPES)}")
    known = set(LoginSpec.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"roles[{role}].login: unknown keys {sorted(unknown)}")
    return LoginSpec(**{**raw, "type": ltype})


def _parse_limits(raw, profile: str) -> Limits:
    raw = _mapping(raw, "limits")
    values = {**_COMMON_LIMITS, **_PROFILE_LIMITS[profile]}
    unknown = set(raw) - set(values)
    if unknown:
        raise ConfigError(f"limits: unknown keys {sorted(unknown)}")
    values.update(raw)
    _num(values, "max_requests_per_second", integer=False, lo=0, lo_open=True)
    _num(values, "jitter_seconds", integer=False, lo=0)
    _num(values, "concurrency", integer=True, lo=1)
    _num(values, "max_states_per_route_pattern", integer=True, lo=1)
    _num(values, "max_runtime_minutes", integer=True, lo=1)
    _num(values, "max_depth", integer=True, lo=1)
    _num(values, "quiet_window_ms", integer=True, lo=0)
    _num(values, "max_form_retries", integer=True, lo=0)
    _num(values, "breaker_window", integer=True, lo=1)
    _num(values, "breaker_threshold", integer=False, lo=0, lo_open=True, hi=1)
    if profile == "production":
        # Safety floor: a file may be gentler than production defaults, never harsher.
        values["concurrency"] = 1
        values["max_requests_per_second"] = min(
            float(values["max_requests_per_second"]), 1.0)
        values["jitter_seconds"] = max(float(values["jitter_seconds"]), 0.5)
    try:
        return Limits(**values)
    except TypeError as e:  # pragma: no cover - guarded by unknown-key check
        raise ConfigError(str(e)) from e


def load_config(path: Path) -> RunConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    raw = _mapping(raw, "config")
    _check_keys(raw, TOP_KEYS, "config")

    target = _mapping(raw.get("target"), "target")
    _check_keys(target, ("base_urls", "scope"), "target")
    for key in ("base_urls", "scope"):
        if key in target and key in raw:
            raise ConfigError(
                f"{key} given both at top level and under target; use target.{key}")
    base_urls = _strs(target.get("base_urls", raw.get("base_urls")),
                      "target.base_urls")
    if not base_urls:
        raise ConfigError("base_urls must list at least one URL")
    hosts = []
    for u in base_urls:
        host = (urlparse(u).hostname or "").lower()
        if not host or urlparse(u).scheme not in ("http", "https"):
            raise ConfigError(f"base_urls entry is not an http(s) URL: {u}")
        if host not in hosts:
            hosts.append(host)

    profile = raw.get("profile", "staging")
    if profile not in PROFILES:
        raise ConfigError("profile must be 'staging' or 'production'")

    auth_raw = raw.get("authorization")
    if not isinstance(auth_raw, dict):
        raise ConfigError("authorization is required (authorization.confirmed_by)")
    _check_keys(auth_raw, ("confirmed_by", "notes"), "authorization")
    confirmed_by = auth_raw.get("confirmed_by")
    if not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise ConfigError("authorization.confirmed_by is required")
    authorization = Authorization(confirmed_by=confirmed_by,
                                  notes=str(auth_raw.get("notes") or ""))

    scope_raw = _mapping(target.get("scope", raw.get("scope")), "target.scope")
    _check_keys(scope_raw, Scope.__dataclass_fields__, "target.scope")
    scope = Scope(
        allow_hosts=[h.lower() for h in _strs(scope_raw.get("allow_hosts"),
                                              "scope.allow_hosts")] or hosts,
        allow_path_prefixes=_strs(scope_raw.get("allow_path_prefixes"),
                                  "scope.allow_path_prefixes"),
        deny_patterns=_strs(scope_raw.get("deny_patterns"), "scope.deny_patterns"),
    )

    disc_raw = _mapping(raw.get("discovery"), "discovery")
    unknown = set(disc_raw) - set(Discovery.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"discovery: unknown keys {sorted(unknown)}")
    defaults = Discovery()
    discovery = Discovery(
        **{k: _bool(disc_raw, k, getattr(defaults, k), "discovery")
           for k in ("passive_sources", "schemas", "bundle_mining",
                     "api_replay", "forced_browsing")},
        wordlists=_strs(disc_raw.get("wordlists"), "discovery.wordlists"),
    )
    if profile == "production" and discovery.forced_browsing:
        raise ConfigError(
            "discovery.forced_browsing is not allowed with profile production")

    roles = []
    roles_raw = raw.get("roles") or []
    if not isinstance(roles_raw, list):
        raise ConfigError("roles must be a list")
    for r in roles_raw:
        r = _mapping(r, "roles entry")
        name = r.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("each role needs a name")
        roles.append(Role(name=name, login=_parse_login(r.get("login"), name)))

    fd_raw = _mapping(raw.get("form_data"), "form_data")
    _check_keys(fd_raw, FormData.__dataclass_fields__, "form_data")
    form_data = FormData(
        marker_prefix=str(fd_raw.get("marker_prefix") or FormData.marker_prefix),
        fixtures=fd_raw.get("fixtures"),
    )

    spa = raw.get("spa", "auto")
    if spa is True:
        spa = "on"
    elif spa is False:  # YAML turns bare on/off into booleans
        spa = "off"
    if spa not in SPA_MODES:
        raise ConfigError("spa must be one of auto, on, off")

    return RunConfig(
        base_urls=base_urls, scope=scope, authorization=authorization,
        profile=profile, limits=_parse_limits(raw.get("limits"), profile),
        roles=roles, discovery=discovery, form_data=form_data, spa=spa,
        redact_patterns=_strs(raw.get("redact_patterns"), "redact_patterns"),
        source_path=path,
    )
