"""Per-role browser sessions: contexts, login flows, loss detection and recovery."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page

from .. import paths
from ..config import Role, RunConfig
from ..events import EventLog
from ..rundir import RunDir
from .recorder import TrafficRecorder, har_part_path

USERNAME_SELECTOR = "input[type=email], input[name*=user i], input[name*=email i]"
PASSWORD_SELECTOR = "input[type=password]"
SUBMIT_SELECTOR = "button[type=submit], input[type=submit]"
LOGIN_TIMEOUT_S = 10.0


@dataclass
class RoleSession:
    role: Role
    context: BrowserContext
    auth_headers: dict[str, str]
    session_storage: dict[str, str]
    recorder: TrafficRecorder | None = None
    rd: RunDir | None = None
    origin: str = ""


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def state_path(rd: RunDir, role: str) -> Path:
    return paths.session_dir(rd.run_id) / f"{role}.json"


def _login_url(cfg: RunConfig, role: Role) -> str:
    return role.login.url or cfg.base_urls[0]


def _spec_state_path(cfg: RunConfig, role: Role) -> Path | None:
    raw = role.login.storage_state_path
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else cfg.source_path.parent / p


_SESSION_STORAGE_INIT = """(entries) => {
  if (!entries) return;
  for (const [k, v] of Object.entries(entries)) {
    if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v);
  }
}"""


async def open_role_session(browser: Browser, cfg: RunConfig, rd: RunDir, role: Role,
                            recorder: TrafficRecorder) -> RoleSession:
    """New context for a role. Pass a headed browser for ``manual`` roles."""
    saved = state_path(rd, role.name)
    source = saved if saved.exists() else None
    if source is None and role.login.type == "storage_state":
        p = _spec_state_path(cfg, role)
        source = p if p and p.exists() else None
    state: dict = {}
    if source is not None:
        state = json.loads(source.read_text(encoding="utf-8"))
    origin = _origin(cfg.base_urls[0])
    kwargs = {}
    if state.get("cookies") or state.get("origins"):
        kwargs["storage_state"] = {"cookies": state.get("cookies", []),
                                   "origins": state.get("origins", [])}
    Path(rd.traffic_dir).mkdir(parents=True, exist_ok=True)
    recorder.finalize_har(role.name)  # merge a part HAR a crashed invocation left behind
    context = await browser.new_context(
        service_workers="block", record_har_path=str(har_part_path(rd, role.name)),
        record_har_content="embed", **kwargs)
    session_storage = dict((state.get("session_storage") or {}).get(origin, {}))
    if session_storage:
        await context.add_init_script(
            script=f"({_SESSION_STORAGE_INIT})(location.origin === {json.dumps(origin)} ? "
                   f"{json.dumps(session_storage)} : null)")
    await recorder.attach(context, role.name)
    return RoleSession(role, context, recorder.auth_headers[role.name], session_storage,
                       recorder=recorder, rd=rd, origin=origin)


# -- state persistence -------------------------------------------------------
async def _save_state(sess: RoleSession, page: Page) -> Path:
    state = await sess.context.storage_state()
    try:
        snap = await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
    except Exception:
        snap = {}
    o = _origin(page.url) if page.url.startswith("http") else sess.origin
    sess.session_storage.clear()
    if o == sess.origin:
        sess.session_storage.update(snap)
    state["session_storage"] = {o: snap}
    path = state_path(sess.rd, sess.role.name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


async def _new_page(sess: RoleSession) -> Page:
    if sess.recorder:
        return await sess.recorder.new_page(sess.context)
    return await sess.context.new_page()


# -- login -------------------------------------------------------------------
async def _visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


async def _await_success(page: Page, sess: RoleSession, login_url: str) -> bool:
    spec = sess.role.login
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    pw_sel = spec.password_selector or PASSWORD_SELECTOR
    while time.monotonic() < deadline:
        if spec.success_selector:
            if await _visible(page, spec.success_selector):
                return True
        elif page.url != login_url and not await _visible(page, pw_sel):
            return True
        await asyncio.sleep(0.1)
    return False


async def _form_login(sess: RoleSession, cfg: RunConfig, page: Page) -> bool:
    spec = sess.role.login
    creds = cfg.credentials(sess.role.name)
    if creds is None:
        return False
    user, password = creds
    login_url = _login_url(cfg, sess.role)
    await sess.context.clear_cookies()
    await page.goto(login_url)
    # Drop a stale token so an SPA shows its login form again.
    cleared = await page.evaluate(
        "() => { const n = localStorage.length + sessionStorage.length;"
        " localStorage.clear(); sessionStorage.clear(); return n; }")
    if cleared:
        await page.reload()
    pw_sel = spec.password_selector or PASSWORD_SELECTOR
    try:
        await page.locator(pw_sel).first.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    await page.locator(spec.username_selector or USERNAME_SELECTOR).first.fill(user)
    await page.locator(pw_sel).first.fill(password)
    await page.locator(spec.submit_selector or SUBMIT_SELECTOR).first.click()
    return await _await_success(page, sess, login_url)


async def _manual_login(sess: RoleSession, cfg: RunConfig, page: Page) -> bool:
    await page.goto(_login_url(cfg, sess.role))
    print(f"[appmap] Log in as role '{sess.role.name}' in the browser window, "
          "then press Enter here to continue.", flush=True)
    await asyncio.to_thread(input)
    return True


async def login(sess: RoleSession, cfg: RunConfig, events: EventLog) -> bool:
    role, spec, rec = sess.role, sess.role.login, sess.recorder
    page: Page | None = None
    ok = False
    if rec:
        rec.origin_by_role[role.name] = "login"
    try:
        if spec.type == "none":
            ok = True
        elif spec.type == "storage_state":
            p = _spec_state_path(cfg, role)
            ok = bool(state_path(sess.rd, role.name).exists() or (p and p.exists()))
            if ok:
                page = await _new_page(sess)
                await page.goto(_login_url(cfg, role))
        else:
            page = await _new_page(sess)
            ok = await (_form_login(sess, cfg, page) if spec.type == "form"
                        else _manual_login(sess, cfg, page))
        if ok and page is not None:
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
    except Exception as exc:
        events.emit("error", role=role.name, message=f"login failed: {exc}")
        ok = False
    finally:
        if rec:
            await rec.drain()
            rec.origin_by_role.pop(role.name, None)
    if ok and page is not None:
        await _save_state(sess, page)
    if page is not None:
        await page.close()
    if ok and rec:
        rec.last_status[role.name] = None
    ev = events.emit("login", role=role.name, ok=ok, login_type=spec.type)
    if rec:
        rec.store.upsert_role(role.name, login_type=spec.type, auth_ok=ok,
                              last_login_event=ev.id)
    return ok


# -- loss detection ----------------------------------------------------------
def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


async def is_session_lost(sess: RoleSession, page: Page, last_status: int | None,
                          login_url: str | None) -> bool:
    spec = sess.role.login
    if spec.type == "none":
        return False
    if last_status == 401:
        return True
    pw_sel = spec.password_selector or PASSWORD_SELECTOR
    # A login form is one password field next to a username field; a change-password
    # page (several password fields, no username) is not a lost session.
    loginish = (await page.locator(pw_sel).count() == 1
                and await _visible(page, pw_sel)
                and await _visible(page, spec.username_selector or USERNAME_SELECTOR))
    if login_url and _path(page.url) == _path(login_url) \
            and urlsplit(page.url).netloc == urlsplit(login_url).netloc:
        if spec.success_selector:
            return loginish or not await _visible(page, spec.success_selector)
        return True
    return loginish


async def ensure_authenticated(sess: RoleSession, page: Page, cfg: RunConfig,
                               events: EventLog) -> bool:
    """Detect a lost session and re-login at most once."""
    rec = sess.recorder
    last_status = rec.last_status.get(sess.role.name) if rec else None
    login_url = sess.role.login.url
    if not await is_session_lost(sess, page, last_status, login_url):
        return True
    events.emit("session_lost", role=sess.role.name, url=page.url, status=last_status)
    sess.auth_headers.clear()
    return await login(sess, cfg, events)
