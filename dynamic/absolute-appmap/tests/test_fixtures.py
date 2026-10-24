import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(method, url, data=None, headers=None, form=False):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=5)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, r.read().decode(), r.headers


def ssr_login(base, user, pw):
    status, _, hdrs = http("POST", base + "/login",
                           {"username": user, "password": pw}, form=True)
    assert status == 302
    return hdrs["Set-Cookie"].split(";")[0]


def test_ssr_login_and_admin_gate(ssr_url):
    admin = ssr_login(ssr_url, "admin", "admin-pw-Fx1")
    alice = ssr_login(ssr_url, "alice", "alice-pw-Fx2")
    assert http("GET", ssr_url + "/admin", headers={"Cookie": admin})[0] == 200
    assert http("GET", ssr_url + "/admin", headers={"Cookie": alice})[0] == 403
    home = http("GET", ssr_url + "/", headers={"Cookie": alice})[1]
    assert "/admin" not in home and "/projects" in home


def test_ssr_new_project_name_length_rule(ssr_url):
    c = ssr_login(ssr_url, "admin", "admin-pw-Fx1")
    status, body, _ = http("POST", ssr_url + "/projects/new",
                           {"name": "a" * 13, "visibility": "public", "due": "2030-01-01"},
                           headers={"Cookie": c}, form=True)
    assert "Name must be at most 12 characters" in body and status in (200, 422)
    status, _, hdrs = http("POST", ssr_url + "/projects/new",
                           {"name": "ok-name", "visibility": "public", "due": "2030-01-01"},
                           headers={"Cookie": c}, form=True)
    assert status == 302


def test_ssr_session_expiry_and_reset(ssr_url):
    c = ssr_login(ssr_url, "alice", "alice-pw-Fx2")
    assert http("GET", ssr_url + "/api/projects/1", headers={"Cookie": c})[0] == 200
    http("POST", ssr_url + "/__expire")
    assert http("GET", ssr_url + "/api/projects/1", headers={"Cookie": c})[0] == 401
    c = ssr_login(ssr_url, "alice", "alice-pw-Fx2")
    for _ in range(205):
        http("GET", ssr_url + "/about", headers={"Cookie": c})
    assert http("GET", ssr_url + "/api/projects", headers={"Cookie": c})[0] == 401


def test_ssr_static_surface(ssr_url):
    assert "Disallow: /secret-report" in http("GET", ssr_url + "/robots.txt")[1]
    sm = http("GET", ssr_url + "/sitemap.xml")[1]
    assert all(p in sm for p in ("/about", "/projects"))
    spec = json.loads(http("GET", ssr_url + "/api/openapi.json")[1])
    assert set(spec["paths"]) == {"/api/projects", "/api/projects/{id}", "/api/internal/stats"}
    s, _, h = http("GET", ssr_url + "/go-external")
    assert s == 302 and h["Location"] == "https://external.invalid/"
    c = ssr_login(ssr_url, "admin", "admin-pw-Fx1")
    pg3 = http("GET", ssr_url + "/projects?page=3", headers={"Cookie": c})[1]
    assert "/projects/21" in pg3
    new = http("GET", ssr_url + "/projects/new", headers={"Cookie": c})[1]
    assert 'pattern="[a-z0-9-]{3,40}"' in new
    detail = http("GET", ssr_url + "/projects/1", headers={"Cookie": c})[1]
    assert "onsubmit=\"return confirm('Are you sure?')\"" in detail


def test_make_cfg_and_run_dir(ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url)
    assert [r.name for r in cfg.roles] == ["admin", "alice", "anonymous"]
    assert cfg.credentials("alice") == ("alice", "alice-pw-Fx2")
    assert run_dir(tmp_path, cfg).root.is_dir()


def test_spa_serves_index_for_deep_link(spa_url):
    s, body, _ = http("GET", spa_url + "/items/3")
    assert s == 200 and '<div id="app">' in body
    js = http("GET", spa_url + "/static/app.js")[1]
    assert '"/api/internal/flags"' in js
    assert http("GET", spa_url + "/static/vendor/vue.global.prod.js")[0] == 200


def spa_token(base, user, pw):
    s, body, _ = http("POST", base + "/api/login", {"username": user, "password": pw})
    assert s == 200
    return json.loads(body)["token"]


def test_spa_auth_and_items(spa_url):
    t1 = spa_token(spa_url, "admin", "admin-pw-Fx1")
    t2 = spa_token(spa_url, "admin", "admin-pw-Fx1")
    assert t1 != t2 and len(t1.split(".")) == 3
    alice = spa_token(spa_url, "alice", "alice-pw-Fx2")
    A = {"Authorization": f"Bearer {t1}"}
    B = {"Authorization": f"Bearer {alice}"}
    assert http("GET", spa_url + "/api/admin/stats", headers=A)[0] == 200
    assert http("GET", spa_url + "/api/admin/stats", headers=B)[0] == 403
    assert http("POST", spa_url + "/api/login", {"username": "x", "password": "y"})[0] == 401
    seen, cursor = 0, ""
    for _ in range(20):
        page = json.loads(http("GET", spa_url + f"/api/items?cursor={cursor}", headers=A)[1])
        seen += len(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == 100
    s, body, _ = http("POST", spa_url + "/api/items", {"title": "new"}, headers=B)
    new_id = json.loads(body)["id"]
    assert isinstance(new_id, int)
    assert http("PUT", spa_url + f"/api/items/{new_id}", {"title": "chg"}, headers=A)[0] == 200
    assert json.loads(http("GET", spa_url + f"/api/items/{new_id}", headers=A)[1])["title"] == "chg"
    http("POST", spa_url + "/__expire")
    assert http("GET", spa_url + "/api/items/1", headers=A)[0] == 401


def test_spa_graphql_ops(spa_url):
    A = {"Authorization": "Bearer " + spa_token(spa_url, "admin", "admin-pw-Fx1")}
    s, body, _ = http("POST", spa_url + "/graphql",
                      {"query": "query ListItems { items { id title } }"}, headers=A)
    items = json.loads(body)["data"]["items"]
    assert s == 200 and len(items) >= 10
    first = items[0]["id"]
    s, body, _ = http("POST", spa_url + "/graphql",
                      {"query": "mutation DeleteItem($id: Int!) { deleteItem(id: $id) }",
                       "variables": {"id": first}}, headers=A)
    assert json.loads(body)["data"]["deleteItem"] is True
    assert http("GET", spa_url + f"/api/items/{first}", headers=A)[0] == 404
    s, body, _ = http("GET", spa_url + "/api/events")
    assert s == 200 and "data:" in body


async def test_spa_renders_in_browser(spa_url, browser):
    page = await browser.new_page()
    await page.goto(spa_url + "/")
    await page.fill("input[name=username]", "alice")
    await page.fill("input[name=password]", "alice-pw-Fx2")
    await page.click("button[type=submit]")
    await page.wait_for_function("localStorage.getItem('token')")
    await page.goto(spa_url + "/items")
    await page.wait_for_selector(".card")
    await page.locator(".card").first.click()
    await page.wait_for_url("**/items/*")
    await page.close()
