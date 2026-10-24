import json
import urllib.request

from appmap import paths
from appmap.browser.recorder import TrafficRecorder
from appmap.browser.session import (ensure_authenticated, login, open_role_session)
from appmap.events import EventLog
from appmap.gate import RequestGate
from appmap.redact import Redactor
from appmap.scope import ScopeGuard
from appmap.store import Store
from appmap.throttle import CircuitBreaker, RateLimiter


class Harness:
    def __init__(self, cfg, rd):
        self.cfg, self.rd = cfg, rd
        secrets = [pw for r in cfg.roles if (c := cfg.credentials(r.name)) for pw in [c[1]]]
        self.redactor = Redactor(secrets, cfg.redact_patterns)
        self.store = Store(rd.db_path)
        self.store.init_schema()
        self.events = EventLog(rd.events_path, self.redactor)
        lim = cfg.limits
        self.gate = RequestGate(ScopeGuard(cfg.scope),
                                RateLimiter(1000, 0),
                                CircuitBreaker(lim.breaker_window, lim.breaker_threshold),
                                self.store)
        self.recorder = TrafficRecorder(self.store, self.events, self.redactor, rd,
                                        self.gate, [])

    async def open(self, browser, name):
        role = next(r for r in self.cfg.roles if r.name == name)
        return await open_role_session(browser, self.cfg, self.rd, role, self.recorder)


def rows(h, sql, *a):
    return [dict(r) for r in h.store.conn.execute(sql, a)]


async def test_form_login_ssr(browser, ssr_url, make_cfg, run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    saved = paths.session_dir(rd.run_id) / "admin.json"
    assert saved.exists()
    data = json.loads(saved.read_text())
    assert any(c["name"] == "sid" for c in data["cookies"])
    assert "session_storage" in data
    assert not list(rd.root.rglob("admin.json"))  # never under the run dir
    role = rows(h, "SELECT * FROM roles WHERE name='admin'")[0]
    assert role["auth_ok"] == 1 and role["login_type"] == "form"
    assert any(e.type == "login" and e.data["ok"] for e in h.events.iter())
    assert {r["origin"] for r in rows(h, "SELECT origin FROM exchanges")} == {"login"}
    await sess.context.close()


async def test_form_login_bad_password(browser, ssr_url, make_cfg, run_dir, tmp_path,
                                       monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("APPMAP_ALICE_PASS", "wrong")
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "alice")
    assert not await login(sess, cfg, h.events)
    assert rows(h, "SELECT auth_ok FROM roles WHERE name='alice'")[0]["auth_ok"] == 0
    await sess.context.close()


async def test_none_login(browser, ssr_url, make_cfg, run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    assert await login(sess, cfg, h.events)
    await sess.context.close()


async def test_spa_bearer_token_learned(browser, spa_url, make_cfg, run_dir, tmp_path,
                                        monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roles = [{"name": "admin", "login": {
        "type": "form", "url": f"{spa_url}/", "username_env": "APPMAP_ADMIN_USER",
        "password_env": "APPMAP_ADMIN_PASS", "success_selector": "text=Signed in as"}}]
    cfg = make_cfg(spa_url, roles=roles)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    assert sess.auth_headers["Authorization"].startswith("Bearer")
    assert sess.session_storage == {}  # the SPA keeps its token in localStorage
    await sess.context.close()


async def test_session_loss_detected_and_recovered(browser, ssr_url, make_cfg, run_dir,
                                                   tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    page = await sess.context.new_page()
    assert (await page.goto(f"{ssr_url}/admin")).status == 200
    assert await ensure_authenticated(sess, page, cfg, h.events)  # still fine: no event
    assert not [e for e in h.events.iter() if e.type == "session_lost"]
    urllib.request.urlopen(urllib.request.Request(
        f"{ssr_url}/__expire", method="POST", data=b""), timeout=5).read()
    await page.goto(f"{ssr_url}/admin")
    assert page.url.endswith("/login")
    assert await ensure_authenticated(sess, page, cfg, h.events)
    assert len([e for e in h.events.iter() if e.type == "session_lost"]) == 1
    assert (await page.goto(f"{ssr_url}/admin")).status == 200
    await sess.context.close()


async def test_change_password_page_is_not_session_loss(browser, ssr_url, make_cfg, run_dir,
                                                        tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    page = await sess.context.new_page()
    await page.goto(f"{ssr_url}/account/password")
    assert await ensure_authenticated(sess, page, cfg, h.events)
    assert not [e for e in h.events.iter() if e.type == "session_lost"]
    await sess.context.close()


async def test_saved_state_restored(browser, ssr_url, make_cfg, run_dir, tmp_path,
                                    monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    await sess.context.close()
    sess2 = await h.open(browser, "admin")
    page = await sess2.context.new_page()
    assert (await page.goto(f"{ssr_url}/admin")).status == 200
    await sess2.context.close()
