"""Field fix 3: a role whose last form login failed is never logged in again by crawl or
probe (lockout risk); only ``appmap auth`` retries it."""
import json

import pytest

from conftest import default_roles

from appmap.cli import _auth
from appmap.crawl import run_crawl
from appmap.probe.replay import run_active

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150, "max_depth": 1}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _login_posts(srv) -> int:
    return sum(1 for m, p in srv.requests if m == "POST" and p == "/login")


async def _failed_auth(make_cfg, run_dir, tmp_path, url, monkeypatch, srv):
    monkeypatch.setenv("APPMAP_ADMIN_PASS", "wrong-password")
    roles = [r for r in default_roles(url) if r["name"] != "alice"]
    cfg = make_cfg(url, roles=roles, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    admin = [r for r in cfg.roles if r.name == "admin"]
    before = _login_posts(srv)
    reasons: dict = {}
    res = await _auth(cfg, rd, admin, False, reasons)
    assert res == {"admin": False}
    assert "Invalid credentials" in reasons["admin"]
    assert _login_posts(srv) == before + 1
    return cfg, rd


async def test_crawl_skips_role_whose_login_failed(browser, ssr_url, _ssr_server, make_cfg,
                                                   run_dir, tmp_path, monkeypatch):
    cfg, rd = await _failed_auth(make_cfg, run_dir, tmp_path, ssr_url, monkeypatch,
                                 _ssr_server)
    n = _login_posts(_ssr_server)
    stats = await run_crawl(cfg, rd, roles=["admin", "anonymous"], budget_minutes=1,
                            browser=browser)
    assert _login_posts(_ssr_server) == n  # no login attempt
    assert stats.status == "done"  # the other role ran; not a stop condition
    assert stats.skipped_roles == ["admin"]
    assert any("role admin: skipped" in w and "appmap auth" in w and "--role admin" in w
               for w in stats.warnings)
    stats = await run_crawl(cfg, rd, roles=["admin", "anonymous"], budget_minutes=1,
                            browser=browser, approved_only=True)
    assert _login_posts(_ssr_server) == n
    assert stats.status == "done"
    # only that role requested: nothing to run -> auth_failed, pointing at appmap auth
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=1, browser=browser)
    assert _login_posts(_ssr_server) == n
    assert stats.status == "auth_failed" and stats.auth_failed == ["admin"]


async def test_probe_skips_role_whose_login_failed(browser, ssr_url, _ssr_server, make_cfg,
                                                   run_dir, tmp_path, monkeypatch):
    cfg, rd = await _failed_auth(make_cfg, run_dir, tmp_path, ssr_url, monkeypatch,
                                 _ssr_server)
    n = _login_posts(_ssr_server)
    res = await run_active(cfg, rd, budget_minutes=2, browser=browser)
    assert _login_posts(_ssr_server) == n
    assert any("role admin: skipped" in w for w in res.warnings)


async def test_never_attempted_role_still_logs_in(browser, ssr_url, _ssr_server, make_cfg,
                                                  run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    n = _login_posts(_ssr_server)
    stats = await run_crawl(cfg, rd, roles=["alice"], budget_minutes=0.2, browser=browser)
    assert _login_posts(_ssr_server) == n + 1
    assert stats.status in ("done", "in_progress") and not stats.skipped_roles
