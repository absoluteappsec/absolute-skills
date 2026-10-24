from pathlib import Path

import pytest
import yaml

from appmap.config import ConfigError, load_config

DATA = Path(__file__).parent / "data"


def _write(tmp_path, **overrides):
    raw = yaml.safe_load((DATA / "config_staging.yaml").read_text())
    raw.update(overrides)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({k: v for k, v in raw.items() if v is not None}))
    return p


def test_staging_defaults():
    cfg = load_config(DATA / "config_staging.yaml")
    assert cfg.limits.max_requests_per_second == 5 and cfg.limits.concurrency == 4
    assert cfg.limits.jitter_seconds == 0
    assert cfg.limits.breaker_threshold == 0.5 and cfg.limits.breaker_window == 20
    assert cfg.limits.max_states_per_route_pattern == 5
    assert cfg.limits.max_runtime_minutes == 240 and cfg.limits.max_depth == 15
    assert cfg.limits.quiet_window_ms == 500 and cfg.limits.max_form_retries == 3
    assert cfg.scope.allow_hosts == ["staging.example.com"]
    assert cfg.form_data.marker_prefix == "appmap-{run_id}"
    assert cfg.spa == "auto"
    assert cfg.source_path == DATA / "config_staging.yaml"
    assert [r.login.type for r in cfg.roles] == ["form", "none"]


def test_production_forces_concurrency_and_rate():
    cfg = load_config(DATA / "config_prod.yaml")
    assert cfg.limits.concurrency == 1 and cfg.limits.max_requests_per_second == 1
    assert cfg.limits.jitter_seconds == 0.5 and cfg.limits.breaker_threshold == 0.2


def test_production_forced_browsing_rejected(tmp_path):
    p = _write(tmp_path, profile="production", discovery={"forced_browsing": True})
    with pytest.raises(ConfigError, match="forced_browsing"):
        load_config(p)


def test_missing_authorization_rejected(tmp_path):
    p = _write(tmp_path, authorization=None)
    with pytest.raises(ConfigError, match="authorization"):
        load_config(p)
    p = _write(tmp_path, authorization={"notes": "x"})
    with pytest.raises(ConfigError, match="confirmed_by"):
        load_config(p)


def test_inline_password_rejected(tmp_path):
    p = _write(tmp_path, roles=[{"name": "a", "login": {"type": "form", "password": "hunter2"}}])
    with pytest.raises(ConfigError, match="credentials must come from env"):
        load_config(p)


def test_invalid_profile_and_login_type_rejected(tmp_path):
    with pytest.raises(ConfigError, match="profile"):
        load_config(_write(tmp_path, profile="dev"))
    with pytest.raises(ConfigError, match="login.type"):
        load_config(_write(tmp_path, roles=[{"name": "a", "login": {"type": "magic"}}]))


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("APPMAP_ADMIN_USER", "admin")
    monkeypatch.setenv("APPMAP_ADMIN_PASS", "pw")
    cfg = load_config(DATA / "config_staging.yaml")
    assert cfg.credentials("admin") == ("admin", "pw")
    assert cfg.credentials("anon") is None


def test_credentials_missing_env_returns_none(monkeypatch):
    monkeypatch.delenv("APPMAP_ADMIN_USER", raising=False)
    monkeypatch.delenv("APPMAP_ADMIN_PASS", raising=False)
    assert load_config(DATA / "config_staging.yaml").credentials("admin") is None


def test_redacted_dict_has_no_env_values(monkeypatch):
    monkeypatch.setenv("APPMAP_ADMIN_USER", "secret-user-value")
    monkeypatch.setenv("APPMAP_ADMIN_PASS", "secret-pass-value")
    cfg = load_config(DATA / "config_staging.yaml")
    d = cfg.to_redacted_dict()
    text = yaml.safe_dump(d)
    assert "APPMAP_ADMIN_USER" in text and "APPMAP_ADMIN_PASS" in text
    assert "secret-user-value" not in text and "secret-pass-value" not in text
    assert isinstance(d["source_path"], str)


def test_spec_example_loads_verbatim():
    cfg = load_config(DATA / "config_spec_example.yaml")
    assert cfg.base_urls == ["https://staging.example.com"]
    assert cfg.scope.allow_hosts == ["staging.example.com", "api.staging.example.com"]
    assert cfg.scope.deny_patterns == ["/admin/billing/*", "*/logout"]
    assert [r.login.type for r in cfg.roles] == ["form", "form", "none", "manual"]
    assert cfg.form_data.fixtures == "./fixtures.yaml"


def test_flat_alias_accepted_and_both_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("base_urls: ['https://a.example.com']\nauthorization: {confirmed_by: me}\n")
    assert load_config(p).scope.allow_hosts == ["a.example.com"]
    p.write_text("base_urls: ['https://a.example.com']\n"
                 "target: {base_urls: ['https://b.example.com']}\n"
                 "authorization: {confirmed_by: me}\n")
    with pytest.raises(ConfigError, match="both"):
        load_config(p)


def test_allow_hosts_lowercased(tmp_path):
    p = _write(tmp_path, target={"base_urls": ["https://A.Example.com"],
                                 "scope": {"allow_hosts": ["A.Example.COM"]}})
    assert load_config(p).scope.allow_hosts == ["a.example.com"]


@pytest.mark.parametrize("override", [
    {"limit": {"concurrency": 2}},
    {"target": {"base_urls": ["https://a.example.com"], "scop": {}}},
    {"target": {"base_urls": ["https://a.example.com"], "scope": {"allow_host": []}}},
    {"authorization": {"confirmed_by": "me", "note": "x"}},
    {"form_data": {"marker": "x"}},
])
def test_unknown_keys_rejected(tmp_path, override):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(_write(tmp_path, **override))


@pytest.mark.parametrize("limits", [
    {"max_requests_per_second": "fast"}, {"max_requests_per_second": 0},
    {"max_requests_per_second": -1}, {"concurrency": 0}, {"concurrency": 1.5},
    {"concurrency": True}, {"breaker_threshold": 0}, {"breaker_threshold": 1.5},
    {"jitter_seconds": -0.1}, {"max_depth": None}, {"max_runtime_minutes": 0},
])
def test_invalid_limits_rejected(tmp_path, limits):
    with pytest.raises(ConfigError, match="limits"):
        load_config(_write(tmp_path, limits=limits))
