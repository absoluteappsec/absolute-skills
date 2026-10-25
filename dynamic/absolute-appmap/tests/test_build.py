"""Task 19: flows, value links, preconditions, names, JSON export, APP-MAP.md, `appmap build`."""
import json
import os
import shutil
import urllib.request
from pathlib import Path

import pytest
import yaml

from appmap.build import (apply_names, derive_flows, derive_preconditions, derive_value_links,
                          emit_naming_stub, export_json, render_app_map)
from appmap.cli import main
from appmap.config import load_config
from appmap.crawl import run_crawl
from appmap.rundir import create_run_dir, open_run_dir, write_run_meta
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}
SECTIONS = ["## Run summary", "## Route tree", "## Endpoint inventory", "## Role access matrix",
            "## Key flows", "## Forms", "## GraphQL operations", "## Unexercised surface",
            "## Coverage gaps", "## Observations", "## Records created by this run",
            "## Where to dig deeper"]


# -- shared fixture crawls (one per module; each test works on its own copy) ----------
async def _crawl(browser, base_url, tmp, roles, crawl_roles):
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    try:
        urllib.request.urlopen(urllib.request.Request(base_url + "/__reset", method="POST",
                                                      data=b""), timeout=5).read()
        raw = {"target": {"base_urls": [base_url]}, "authorization": {"confirmed_by": "test"},
               "profile": "staging", "roles": roles, "limits": FAST}
        cfg_path = tmp / "cfg.yaml"
        cfg_path.write_text(yaml.safe_dump(raw))
        cfg = load_config(cfg_path)
        rd = create_run_dir(cfg, tmp / "runs")
        write_run_meta(rd, cfg, status="done", extra={"config_path": str(cfg_path)})
        await run_crawl(cfg, rd, roles=crawl_roles, budget_minutes=5, browser=browser)
        return rd.root
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


@pytest.fixture(scope="module")
async def ssr_run(browser, _ssr_server, tmp_path_factory):
    url = _ssr_server.url
    roles = [{"name": n, "login": {"type": "form", "url": f"{url}/login",
                                   "username_env": f"APPMAP_{n.upper()}_USER",
                                   "password_env": f"APPMAP_{n.upper()}_PASS"}}
             for n in ("admin", "alice")]
    return await _crawl(browser, url, tmp_path_factory.mktemp("ssr"), roles, ["admin", "alice"])


@pytest.fixture(scope="module")
async def spa_run(browser, _spa_server, tmp_path_factory):
    url = _spa_server.url
    roles = [{"name": "admin", "login": {
        "type": "form", "url": f"{url}/", "username_env": "APPMAP_ADMIN_USER",
        "password_env": "APPMAP_ADMIN_PASS", "success_selector": "text=Signed in as"}}]
    return await _crawl(browser, url, tmp_path_factory.mktemp("spa"), roles, ["admin"])


def _copy(src: Path, tmp_path: Path) -> Path:
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def _rows(store, sql, *args):
    return [dict(r) for r in store.conn.execute(sql, args)]


def _status(capsys) -> dict:
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("APPMAP_STATUS ")
    return json.loads(last.removeprefix("APPMAP_STATUS "))


# -- brief tests ---------------------------------------------------------------------
def test_flow_for_new_project(ssr_run, tmp_path):
    root = _copy(ssr_run, tmp_path)
    store = Store(root / "appmap.sqlite")
    try:
        assert derive_flows(store) >= 2  # admin and alice each created a project
        flows = _rows(store, "SELECT * FROM flows WHERE role='admin'")
        hit = None
        for f in flows:
            steps = _rows(store, """SELECT fs.*, x.method, e.path_pattern FROM flow_steps fs
                                    LEFT JOIN exchanges x ON x.id=fs.exchange_id
                                    LEFT JOIN endpoints e ON e.id=x.endpoint_id
                                    WHERE fs.flow_id=? ORDER BY fs.ordinal""", f["id"])
            if steps and (steps[-1]["method"], steps[-1]["path_pattern"]) == \
                    ("POST", "/projects/new"):
                hit = (f, steps)
        assert hit, flows
        flow, steps = hit
        assert flow["name"] == "admin: Create @ /projects/new"
        assert [s["ordinal"] for s in steps] == list(range(1, len(steps) + 1))
        assert len(steps) >= 2 and steps[0]["transition_id"]  # navigation to the form first
        sub = store.conn.execute("SELECT action_type FROM transitions WHERE id=?",
                                 (steps[-1]["transition_id"],)).fetchone()[0]
        assert sub == "submit"
        pre = json.loads(flow["preconditions"])
        assert pre["role"] == "admin" and pre["origin"] == "ui"
        assert pre["flow_key"] == "admin|POST /projects/new"
        assert pre["steps"][-1]["mutating"] is True and pre["steps"][0]["mutating"] is False
        assert "[mutating]" in flow["description"]
    finally:
        store.close()


def test_value_link_created_id_used(spa_run, tmp_path):
    root = _copy(spa_run, tmp_path)
    store = Store(root / "appmap.sqlite")
    try:
        post = _rows(store, "SELECT * FROM exchanges WHERE method='POST' "
                            "AND url LIKE '%/api/items' AND status=201")[0]
        new_id = json.loads(post["resp_body"])["id"]
        base = post["url"].rsplit("/api/items", 1)[0]
        host = base.split("://", 1)[1]
        ep = store.upsert_endpoint("GET", host, "/api/items/{id}", "rest")
        later = store.add_exchange(ep, "admin", "replay", method="GET",
                                   url=f"{base}/api/items/{new_id}", status=200,
                                   resp_body=json.dumps({"id": new_id}),
                                   content_type="application/json")
        assert derive_value_links(store) >= 1
        links = _rows(store, "SELECT * FROM value_links WHERE consumer_exchange_id=?", later)
        assert {(l["producer_exchange_id"], l["producer_json_path"], l["consumer_location"])
                for l in links} == {(post["id"], "$.id", "path")}
        # the created item's title is the run marker: never a link value
        marker_links = _rows(store, "SELECT * FROM value_links WHERE producer_json_path='$.title' "
                                    "AND producer_exchange_id=?", post["id"])
        assert marker_links == []
    finally:
        store.close()


def test_build_idempotent(ssr_run, tmp_path, capsys):
    root = _copy(ssr_run, tmp_path)
    assert main(["build", "--run", str(root)]) == 0
    st = _status(capsys)
    assert st["ok"] and st["command"] == "build" and st["status"] == "complete"
    first = (st["counts"]["flows"], st["counts"]["flow_steps"], st["counts"]["value_links"])
    assert first[0] >= 2
    assert main(["build", "--run", str(root)]) == 0
    st = _status(capsys)
    assert (st["counts"]["flows"], st["counts"]["flow_steps"], st["counts"]["value_links"]) == first
    store = Store(root / "appmap.sqlite")
    try:
        assert store.conn.execute("SELECT count(*) FROM flows").fetchone()[0] == first[0]
    finally:
        store.close()
    for name in ("appmap.json", "APP-MAP.md", "SCHEMA.md", "names.yaml"):
        assert (root / name).exists(), name
    assert json.loads((root / "run.json").read_text())["status"] == "complete"
    pkg = Path(__file__).parents[1] / "lib" / "appmap" / "resources" / "SCHEMA.md"
    assert (root / "SCHEMA.md").read_text() == pkg.read_text()
    assert st["next"] == f"edit {root / 'names.yaml'}, then re-run: appmap build --run {root}"


def test_app_map_sections_present(ssr_run, tmp_path, capsys):
    root = _copy(ssr_run, tmp_path)
    assert main(["build", "--run", str(root)]) == 0
    capsys.readouterr()
    md = (root / "APP-MAP.md").read_text()
    pos = [md.find(s) for s in SECTIONS]
    assert all(p >= 0 for p in pos), [s for s, p in zip(SECTIONS, pos) if p < 0]
    assert pos == sorted(pos)
    assert "Handling notice" not in md  # staging run
    assert "/projects/{id}" in md and "admin: Create @ /projects/new" in md
    assert "POST /projects/new" in md
    # the SSR name field only had room for "appmap-20261" (no full run date): such a
    # truncation is not run-specific, so the created project is not claimed
    created = md.split("## Records created by this run", 1)[1].split("## Where", 1)[0]
    assert "No records identifiably created by this run." in created
    for secret in ("admin-pw-Fx1", "alice-pw-Fx2"):
        assert secret not in md
        assert secret not in (root / "appmap.json").read_text()


def test_json_export_round_trip(ssr_run, tmp_path):
    root = _copy(ssr_run, tmp_path)
    store = Store(root / "appmap.sqlite")
    try:
        store.set_meta("approval_failed:ap-0001", "x")
        store.set_meta("approval_inflight:ap-0002", "ev-1")
        store.set_meta("pass", "2")
        store.set_meta("pass_2_baseline", "{}")
        store.set_meta("pass_2_baseline_ts", "1.0")
        store.set_meta("last_activity_ts", "1.0")
        derive_value_links(store)
        derive_flows(store)
        derive_preconditions(store)
        export_json(store, root / "appmap.json")
        tables = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        data = json.loads((root / "appmap.json").read_text())
        assert set(data) - {"schema_version", "run"} == tables - {"frontier", "frontier_links"}
        assert data["schema_version"] == "1.0.0"
        assert data["run"]["run_id"] == json.loads((root / "run.json").read_text())["run_id"]
        keys = {m["key"] for m in data["meta"]}
        assert "schema_version" in keys
        assert not {k for k in keys if k.startswith(("approval_", "pass")) or
                    k == "last_activity_ts"}
        for t in tables - {"frontier", "frontier_links", "meta"}:
            assert len(data[t]) == store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        # JSON columns are decoded
        assert isinstance(data["flows"][0]["preconditions"], dict)
        assert all(isinstance(t["exchange_ids"], (list, type(None))) for t in data["transitions"])
    finally:
        store.close()


# -- additional behaviour ------------------------------------------------------------
def _synthetic(tmp_path, *, profile="staging") -> tuple[Store, Path]:
    root = tmp_path / "appmap-example.test-20261024-120000"
    root.mkdir()
    (root / "run.json").write_text(json.dumps({
        "run_id": "20261024-120000", "profile": profile, "schema_version": "1.0.0",
        "contains_production_data": profile == "production", "status": "done",
        "roles": ["u"], "config": {"base_urls": ["https://example.test"],
                                   "form_data": {"marker_prefix": "appmap-{run_id}"}}}))
    (root / "events.jsonl").write_text("")
    store = Store(root / "appmap.sqlite")
    store.init_schema()
    store.upsert_role("u", "form", True)
    return store, root


def _x(store, role, method, url, *, status=200, req_body=None, resp=None, ep=None):
    return store.add_exchange(ep, role, "ui", method=method, url=url, status=status,
                              req_body=req_body,
                              resp_body=json.dumps(resp) if resp is not None else None,
                              content_type="application/json" if resp is not None else "text/html")


def test_value_links_skip_placeholders_marker_and_common_values(tmp_path):
    store, _root = _synthetic(tmp_path)
    try:
        b = "https://example.test"
        p = _x(store, "u", "GET", f"{b}/api/me", resp={
            "id": 1, "zero": 0, "org": 7, "token": "<redacted:secret>", "slug": "appmap-20261024-120000",
            "short": "abc", "flag": True, "kind": "admin", "ref": "acme-co"})
        _x(store, "u", "GET", f"{b}/api/x/1/0/<redacted:secret>/appmap-20261024-120000/abc/admin")
        c1 = _x(store, "u", "GET", f"{b}/api/orgs/7?ref=acme-co")
        for i in range(3):  # 7 appears in most later requests: too common to be provenance
            _x(store, "u", "GET", f"{b}/api/page/7?n={i}")
        derive_value_links(store, marker="appmap-20261024-120000")
        got = {(r["producer_json_path"], r["consumer_exchange_id"], r["consumer_location"])
               for r in _rows(store, "SELECT * FROM value_links")}
        assert got == {("$.ref", c1, "query")}
        assert all(r["producer_exchange_id"] == p for r in _rows(store, "SELECT * FROM value_links"))
    finally:
        store.close()


def test_value_link_in_json_body_and_not_before_producer(tmp_path):
    store, _root = _synthetic(tmp_path)
    try:
        b = "https://example.test"
        early = _x(store, "u", "GET", f"{b}/api/things/4242")
        p = _x(store, "u", "POST", f"{b}/api/teams", req_body='{"name": "Red Team"}',
               resp={"team": {"id": 9001, "name": "Red Team"}, "thing": 4242}, status=201)
        c = _x(store, "u", "PUT", f"{b}/api/members", req_body='{"team_id": 9001}')
        _x(store, "u", "GET", f"{b}/api/things/4242")  # known before the producer: no link
        other_role = _x(store, "v", "GET", f"{b}/api/teams/9001")
        derive_value_links(store)
        links = _rows(store, "SELECT * FROM value_links")
        assert [(l["producer_exchange_id"], l["producer_json_path"], l["consumer_exchange_id"],
                 l["consumer_location"]) for l in links] == [(p, "$.team.id", c, "body")]
        assert early < p and other_role not in {l["consumer_exchange_id"] for l in links}
        assert links[0]["value_hash"] and "9001" not in links[0]["value_hash"]
    finally:
        store.close()


def _state(store, sid, role, url, pattern):
    store.upsert_state(sid, role, url, route_pattern=pattern)


def _frontier(store, role, path, status="done"):
    store.conn.execute("INSERT INTO frontier(role, kind, url, action_path, status) "
                       "VALUES(?, 'action', ?, ?, ?)",
                       (role, path[0].get("url", ""), json.dumps(path), status))


def _events(root, *events):
    with open(root / "events.jsonl", "a") as f:
        for ev in events:
            f.write(json.dumps({"ts": 1.0, "caused_by": None, "data": {}, **ev}) + "\n")


def test_wizard_submits_merge_and_preconditions(tmp_path):
    store, root = _synthetic(tmp_path)
    try:
        b = "https://example.test"
        ep_list = store.upsert_endpoint("GET", "example.test", "/api/plans", "rest")
        ep1 = store.upsert_endpoint("POST", "example.test", "/signup/step1", "page")
        ep2 = store.upsert_endpoint("POST", "example.test", "/signup/step2", "page")
        _state(store, "s1", "u", f"{b}/signup", "/signup")
        _state(store, "s2", "u", f"{b}/signup/2", "/signup/2")
        _state(store, "s3", "u", f"{b}/welcome", "/welcome")
        f1 = store.add_form("s1", method="POST", action_endpoint_id=ep1, succeeded=True)
        f2 = store.add_form("s2", method="POST", action_endpoint_id=ep2, succeeded=True)
        plans = _x(store, "u", "GET", f"{b}/api/plans", resp={"plans": [{"code": "gold-plan"}]},
                   ep=ep_list)
        nav = store.add_transition(None, "s1", "u", "navigate", "Sign up", exchange_ids=[])
        x1 = _x(store, "u", "POST", f"{b}/signup/step1", status=302, ep=ep1,
                req_body="plan=gold-plan&name=appmap-20261024-120000")
        t1 = store.add_transition("s1", "s2", "u", "submit", "Next", event_id="ev-000001",
                                  exchange_ids=[x1])
        x2 = _x(store, "u", "POST", f"{b}/signup/step2", status=302, ep=ep2, req_body="ok=yes")
        t2 = store.add_transition("s2", "s3", "u", "submit", "Finish", event_id="ev-000003",
                                  exchange_ids=[x2])
        # sequential submits of one session (another role's submit in between is fine)
        _events(root, {"id": "ev-000001", "type": "submit", "role": "u"},
                {"id": "ev-000002", "type": "submit", "role": "v"},
                {"id": "ev-000003", "type": "submit", "role": "u"})
        _frontier(store, "u", [{"type": "goto", "url": f"{b}/signup"},
                               {"type": "submit", "form_label": "Next", "form_id": f1}])
        _frontier(store, "u", [{"type": "goto", "url": f"{b}/signup/2"},
                               {"type": "submit", "form_label": "Finish", "form_id": f2}])
        derive_value_links(store)
        assert derive_flows(store) == 3  # both standalone steps plus the merged wizard
        names = [r["name"] for r in _rows(store, "SELECT name FROM flows ORDER BY id")]
        assert names == ["u: Next @ /signup", "u: Finish @ /signup/2", "u: Next → Finish @ /signup"]
        flow = _rows(store, "SELECT * FROM flows WHERE name LIKE '%→%'")[0]
        steps = _rows(store, "SELECT * FROM flow_steps WHERE flow_id=? ORDER BY ordinal", flow["id"])
        assert [(s["transition_id"], s["exchange_id"]) for s in steps] == \
            [(nav, None), (t1, x1), (t2, x2)]
        pre = json.loads(flow["preconditions"])
        assert pre["flow_key"] == "u|POST /signup/step1|POST /signup/step2"
        assert pre["form_ids"] == [f1, f2]
        assert derive_preconditions(store) == 2  # the Next flow and the merged flow
        pre = json.loads(_rows(store, "SELECT preconditions FROM flows WHERE id=?",
                               flow["id"])[0]["preconditions"])
        assert pre["requires"] == [{"endpoint": "GET /api/plans", "json_path": "$.plans[0].code",
                                    "producer_exchange_id": plans, "consumer_exchange_id": x1,
                                    "consumer_location": "body"}]
    finally:
        store.close()


def test_executed_approval_becomes_flow(tmp_path):
    store, root = _synthetic(tmp_path)
    try:
        b = "https://example.test"
        ep = store.upsert_endpoint("POST", "example.test", "/projects/{id}/archive", "page")
        _state(store, "p1", "u", f"{b}/projects/3", "/projects/{id}")
        _state(store, "p2", "u", f"{b}/projects", "/projects")
        x = _x(store, "u", "POST", f"{b}/projects/3/archive", status=302, ep=ep)
        t = store.add_transition("p1", "p2", "u", "click", "Archive", event_id="ev-000010",
                                 exchange_ids=[x])
        path = [{"type": "goto", "url": f"{b}/projects/3"},
                {"type": "click", "label": "Archive", "locators": []}]
        store.upsert_approval("ap-0001", role="u", category="deactivate", label="Archive",
                              action_path=path, decision="approve", executed_event="ev-000011")
        store.upsert_approval("ap-0002", role="u", category="delete", label="Delete",
                              action_path=path, decision="approve", executed_event="ev-000013")
        store.set_meta("approval_failed:ap-0002", "approved action: boom")
        with open(root / "events.jsonl", "w") as f:
            for ev in ({"id": "ev-000011", "ts": 1.0, "type": "approval_executed", "role": "u",
                        "caused_by": "ev-000010", "data": {"approval_id": "ap-0001",
                                                           "exchange_ids": [x], "outcome": "done"}},
                       {"id": "ev-000013", "ts": 2.0, "type": "approval_executed", "role": "u",
                        "caused_by": "ev-000012", "data": {"approval_id": "ap-0002",
                                                           "exchange_ids": [], "outcome": "error"}}):
                f.write(json.dumps(ev) + "\n")
        assert derive_flows(store) == 1
        flow = _rows(store, "SELECT * FROM flows")[0]
        assert flow["name"] == "u: Archive @ /projects/{id}"
        pre = json.loads(flow["preconditions"])
        assert pre["origin"] == "approved" and pre["approval_id"] == "ap-0001"
        assert pre["steps"][-1]["mutating"] is True
        steps = _rows(store, "SELECT * FROM flow_steps WHERE flow_id=? ORDER BY ordinal", flow["id"])
        assert (steps[-1]["transition_id"], steps[-1]["exchange_id"]) == (t, x)
    finally:
        store.close()


def test_names_match_by_key_and_survive_rebuild(ssr_run, tmp_path, capsys):
    root = _copy(ssr_run, tmp_path)
    assert main(["build", "--run", str(root)]) == 0
    capsys.readouterr()
    stub = yaml.safe_load((root / "names.yaml").read_text())
    entry = next(f for f in stub["flows"] if f["key"] == "admin|POST /projects/new")
    assert entry["description"] == "" and {"id", "name", "key"} <= set(entry)
    assert stub["forms"] and {"id", "label", "purpose"} <= set(stub["forms"][0])
    form = next(f for f in stub["forms"] if f["label"] == "Create")
    named = {"flows": [{"id": 9999, "key": entry["key"], "name": "Create a project",
                        "description": "Admin creates a project from /projects/new"}],
             "forms": [{"id": form["id"], "label": "Create", "purpose": "create project"}]}
    names = tmp_path / "names.yaml"
    names.write_text(yaml.safe_dump(named))
    for _ in range(2):  # re-applying after a rebuild keeps matching the same flow
        assert main(["build", "--run", str(root), "--names", str(names)]) == 0
        st = _status(capsys)
        assert st["ok"] and st["counts"]["names_applied"] >= 2 and st["next"] is None
        store = Store(root / "appmap.sqlite")
        try:
            f = _rows(store, "SELECT * FROM flows WHERE name='Create a project'")
            assert len(f) == 1 and json.loads(f[0]["preconditions"])["flow_key"] == entry["key"]
            assert f[0]["description"] == "Admin creates a project from /projects/new"
            assert store.conn.execute("SELECT count(*) FROM forms WHERE purpose='create project'"
                                      ).fetchone()[0] >= 1
        finally:
            store.close()
    md = (root / "APP-MAP.md").read_text()
    assert "Create a project" in md and "create project" in md
    # the run-dir stub is not overwritten by a rebuild (edits there are kept and applied)
    stub_edit = yaml.safe_load((root / "names.yaml").read_text())
    stub_edit["flows"][0]["name"] = "Edited in stub"
    (root / "names.yaml").write_text(yaml.safe_dump(stub_edit, sort_keys=False))
    assert main(["build", "--run", str(root)]) == 0
    capsys.readouterr()
    assert yaml.safe_load((root / "names.yaml").read_text())["flows"][0]["name"] == "Edited in stub"
    store = Store(root / "appmap.sqlite")
    try:
        assert store.conn.execute("SELECT count(*) FROM flows WHERE name='Edited in stub'"
                                  ).fetchone()[0] == 1
    finally:
        store.close()


def test_apply_names_ignores_unknown_and_malformed(tmp_path):
    store, root = _synthetic(tmp_path)
    try:
        p = root / "n.yaml"
        p.write_text("flows: [{key: nope, name: x}, 3, {id: 77, name: y}]\nforms: [{id: 5}]\n")
        assert apply_names(store, p) == 0
        p.write_text(": not yaml : [")
        with pytest.raises(ValueError):
            apply_names(store, p)
        emit_naming_stub(store, root / "names.yaml")
        assert yaml.safe_load((root / "names.yaml").read_text()) == {"flows": [], "forms": []}
    finally:
        store.close()


def test_app_map_production_notice_and_caps(tmp_path):
    store, root = _synthetic(tmp_path, profile="production")
    try:
        for i in range(150):
            store.upsert_endpoint("GET", "example.test", f"/api/r{i}", "rest")
        md = render_app_map(store, open_run_dir(root))
        notice = md.find("Handling notice")
        assert 0 <= notice < md.find("## Run summary")
        assert "production customer data" in md
        assert "more — see appmap.sqlite" in md
        assert md.count("| GET | `/api/r") < 150
    finally:
        store.close()


# -- SCHEMA.md ---------------------------------------------------------------------
SCHEMA_MD = Path(__file__).parents[1] / "lib" / "appmap" / "resources" / "SCHEMA.md"
RECIPES = ["flows", "forms-posting-to", "hidden-but-reachable", "endpoint-shape",
           "path-to-state", "role-matrix", "gaps"]


def _recipes() -> dict[str, str]:
    import re

    text = SCHEMA_MD.read_text().split("## Query recipes", 1)[1]
    return dict(re.findall(r"### `([\w-]+)`\n.*?```sql\n(.*?)```", text, re.S))


def test_schema_md_documents_every_table_and_column(tmp_path):
    store, _root = _synthetic(tmp_path)
    try:
        text = SCHEMA_MD.read_text()
        for (table,) in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            assert f"`{table}`" in text, table
            for col in store.conn.execute(f"PRAGMA table_info({table})"):
                assert f"`{col['name']}`" in text or f"`{table}.{col['name']}`" in text, \
                    (table, col["name"])
        for vocab in ("approval_failed:", "approval_inflight:", "pass_<n>_baseline",
                      "last_activity_ts", "different_content", "well_known", "cross_role"):
            assert vocab in text
    finally:
        store.close()


def test_schema_md_recipes_run(ssr_run, spa_run, tmp_path):
    recipes = _recipes()
    assert set(RECIPES) <= set(recipes)
    for run, params in ((ssr_run, {"role": "admin", "endpoint": "/projects/%", "method": "post",
                                   "path": "/projects/new"}),
                        (spa_run, {"role": "admin", "endpoint": "/api/items", "method": "get",
                                   "path": "/api/items/{id}"})):
        root = _copy(run, tmp_path / run.parent.parent.name)
        store = Store(root / "appmap.sqlite")
        try:
            derive_value_links(store)
            derive_flows(store)
            click = store.conn.execute("SELECT to_state FROM transitions "
                                       "WHERE action_type='click' LIMIT 1").fetchone()
            params["state"] = click[0] if click else \
                store.conn.execute("SELECT id FROM states LIMIT 1").fetchone()[0]
            out = {n: [dict(r) for r in store.conn.execute(recipes[n], params)] for n in RECIPES}
            assert out["flows"] and out["forms-posting-to"] and out["endpoint-shape"]
            assert out["path-to-state"][0]["action_type"] == "goto"
            if click:  # reached by a click: the path ends with that click and its locators
                last = out["path-to-state"][-1]
                assert last["action_type"] == "click" and json.loads(last["locators"])
        finally:
            store.close()


# -- fix round 1 -----------------------------------------------------------------------
def _two_forms(tmp_path, *, cycle: bool, sequential: bool):
    """Reviewer repro: "Add" on /items/new lands on /items, whose Filter form posts."""
    store, root = _synthetic(tmp_path)
    b = "https://example.test"
    ep_a = store.upsert_endpoint("POST", "example.test", "/items", "page")
    ep_b = store.upsert_endpoint("POST", "example.test", "/items/filter", "page")
    _state(store, "sl", "u", f"{b}/items", "/items")
    _state(store, "sn", "u", f"{b}/items/new", "/items/new")
    fa = store.add_form("sn", method="POST", action_endpoint_id=ep_a, succeeded=True)
    fb = store.add_form("sl", method="POST", action_endpoint_id=ep_b, succeeded=True)
    xa = _x(store, "u", "POST", f"{b}/items", status=302, ep=ep_a)
    store.add_transition("sn", "sl", "u", "submit", "Add", event_id="ev-000001", exchange_ids=[xa])
    xb = _x(store, "u", "POST", f"{b}/items/filter", status=302, ep=ep_b)
    store.add_transition("sl", "sn" if cycle else "sl", "u", "submit", "Filter",
                         event_id="ev-000003", exchange_ids=[xb])
    _frontier(store, "u", [{"type": "goto", "url": f"{b}/items/new"},
                           {"type": "submit", "form_label": "Add", "form_id": fa}])
    _frontier(store, "u", [{"type": "goto", "url": f"{b}/items"},
                           {"type": "submit", "form_label": "Filter", "form_id": fb}])
    evs = [{"id": "ev-000001", "type": "submit", "role": "u"}]
    if not sequential:
        evs.append({"id": "ev-000002", "type": "submit", "role": "u"})
    evs.append({"id": "ev-000003", "type": "submit", "role": "u"})
    _events(root, *evs)
    return store


@pytest.mark.parametrize("cycle", [False, True])
def test_wizard_merge_needs_sequential_submits_and_never_drops_flows(tmp_path, cycle):
    store = _two_forms(tmp_path, cycle=cycle, sequential=False)
    try:
        assert derive_flows(store) == 2
        assert {r["name"] for r in _rows(store, "SELECT name FROM flows")} == \
            {"u: Add @ /items/new", "u: Filter @ /items"}
    finally:
        store.close()


@pytest.mark.parametrize("cycle", [False, True])
def test_wizard_merge_is_additive_and_acyclic(tmp_path, cycle):
    store = _two_forms(tmp_path, cycle=cycle, sequential=True)
    try:
        assert derive_flows(store) == 3
        assert {r["name"] for r in _rows(store, "SELECT name FROM flows")} == \
            {"u: Add @ /items/new", "u: Filter @ /items", "u: Add → Filter @ /items/new"}
    finally:
        store.close()


def test_approved_submit_flow_has_form_id(tmp_path):
    store, root = _synthetic(tmp_path)
    try:
        b = "https://example.test"
        ep = store.upsert_endpoint("POST", "example.test", "/account/email", "page")
        _state(store, "a1", "u", f"{b}/account", "/account")
        fid = store.add_form("a1", method="POST", action_endpoint_id=ep)
        x = _x(store, "u", "POST", f"{b}/account/email", status=302, ep=ep)
        store.add_transition("a1", "a1", "u", "submit", "Send", event_id="ev-000010",
                             exchange_ids=[x])
        store.upsert_approval("ap-0001", role="u", category="outbound_message", label="Send",
                              action_path=[{"type": "goto", "url": f"{b}/account"},
                                           {"type": "submit", "form_label": "Send",
                                            "form_id": fid, "method": "POST"}],
                              decision="approve", executed_event="ev-000011")
        _events(root, {"id": "ev-000010", "type": "submit", "role": "u"},
                {"id": "ev-000011", "type": "approval_executed", "role": "u",
                 "caused_by": "ev-000010", "data": {"exchange_ids": [x], "outcome": "done"}})
        assert derive_flows(store) == 1
        pre = json.loads(_rows(store, "SELECT preconditions FROM flows")[0]["preconditions"])
        assert pre["form_ids"] == [fid] and pre["origin"] == "approved"
    finally:
        store.close()


def test_goto_step_carries_page_load_shape(ssr_run, tmp_path):
    root = _copy(ssr_run, tmp_path)
    store = Store(root / "appmap.sqlite")
    try:
        derive_flows(store)
        pre = json.loads(_rows(store, "SELECT preconditions FROM flows WHERE role='admin'")[0]
                         ["preconditions"])
        goto = pre["steps"][0]
        assert goto["type"] == "goto" and goto["method"] == "GET"
        assert (goto["path_pattern"], goto["status"]) == ("/projects/new", 200)
    finally:
        store.close()


def test_path_to_state_sql_is_fast_on_dense_graph(tmp_path):
    import random
    import time
    from collections import deque

    store, _root = _synthetic(tmp_path)
    try:
        rnd = random.Random(7)
        n = 200
        for i in range(n):
            _state(store, f"s{i}", "u", f"https://example.test/p?i={i}", "/p")
        store.add_transition(None, "s0", "u", "navigate", "home")  # the only URL entry
        edges = set()
        for i in range(n):  # a ring gives every state a way in, so s0 is the only entry
            for j in [(i + 1) % n, *rnd.sample(range(n), 3)]:
                if j != i and (i, j) not in edges:
                    edges.add((i, j))
                    store.add_transition(f"s{i}", f"s{j}", "u", "click", f"{i}->{j}")
        target = "s199"
        dist = {0: 0}
        q = deque([0])
        while q:  # forward BFS from the entry for the expected length
            i = q.popleft()
            for a, c in edges:
                if a == i and c not in dist:
                    dist[c] = dist[i] + 1
                    q.append(c)
        sql = _recipes()["path-to-state"]
        t0 = time.monotonic()
        rows = [dict(r) for r in store.conn.execute(sql, {"state": target})]
        assert time.monotonic() - t0 < 2.0
        assert rows[0]["action_type"] == "goto" and rows[0]["url"].endswith("i=0")
        assert len(rows) - 1 == dist[199]
        cur = "s0"
        for r in rows[1:]:  # a connected chain of clicks ending at the target
            t = store.conn.execute("SELECT * FROM transitions WHERE id=?",
                                   (r["transition_id"],)).fetchone()
            assert t["from_state"] == cur
            cur = t["to_state"]
        assert cur == target
    finally:
        store.close()


def test_apply_names_ignores_unhashable_entries(tmp_path):
    store, root = _synthetic(tmp_path)
    try:
        p = root / "n.yaml"
        p.write_text("flows: [{key: [a], name: x}, {id: {a: 1}, name: y}, {key: {a: 1}}]\n"
                     "forms: [{id: [1], purpose: p}, {id: {a: 1}, purpose: q}]\n")
        assert apply_names(store, p) == 0
    finally:
        store.close()


def _record_ids(store, root) -> set[int]:
    md = render_app_map(store, open_run_dir(root))
    rec = md.split("## Records created by this run", 1)[1].split("## Where", 1)[0]
    return {int(line.split("|")[1]) for line in rec.splitlines()
            if line.startswith("| ") and line.split("|")[1].strip().isdigit()}


MARKER = "appmap-20261024-120000"


def test_records_match_this_runs_truncations_per_field_kind(tmp_path):
    from appmap.build import marker_variants

    store, root = _synthetic(tmp_path)
    try:
        b, m = "https://example.test", MARKER
        fid = store.add_form(None, method="POST")
        store.add_field(fid, "name", "text", "input",
                        validation_messages=["Name must be at most 16 characters"])
        store.add_field(fid, "email", "email", "input", constraints={"maxlength": 30})
        store.add_field(fid, "pw", "password", "input", constraints={"maxlength": 22})
        store.add_field(fid, "site", "url", "input", constraints={"maxlength": 38})
        assert marker_variants(store, m) == {m, m[:16], m[:17]}
        text = _x(store, "u", "POST", f"{b}/projects", status=302, req_body=f"name={m[:16]}")
        email = _x(store, "u", "POST", f"{b}/invite", status=201,
                   req_body=f"email={m[:17]}%40example.test")
        pw = _x(store, "u", "POST", f"{b}/pw", status=201, req_body=f"pw={m[:17]}-Aa1%21")
        url = _x(store, "u", "POST", f"{b}/site", status=201,
                 req_body=f"site=https%3A%2F%2Fexample.test%2F{m[:17]}")
        query = _x(store, "u", "POST", f"{b}/tags?label={m}", status=201)
        bodies = root / "bodies"
        bodies.mkdir()
        big = store.add_exchange(None, "u", "ui", method="PUT", url=f"{b}/docs/1", status=200,
                                 req_body_path="bodies/9.req.json")
        (bodies / "9.req.json").write_text(json.dumps({"title": m, "pad": "x" * 10}))
        # not ours: other prefixes, failed status, marker only in the URL path
        _x(store, "u", "POST", f"{b}/projects", status=302, req_body="name=appmap-2026")
        _x(store, "u", "POST", f"{b}/projects", status=422, req_body=f"name={m}")
        _x(store, "u", "POST", f"{b}/q", status=201, req_body="name=appmap%25_20261024")
        _x(store, "u", "POST", f"{b}/items/{m}/rename", status=200, req_body="x=1")
        assert _record_ids(store, root) == {text, email, pw, url, query, big}
    finally:
        store.close()


def test_records_ignore_short_truncations_and_url_paths(tmp_path):
    from appmap.build import marker_variants

    store, root = _synthetic(tmp_path)
    try:
        b, m = "https://example.test", MARKER
        fid = store.add_form(None, method="POST")
        # reviewer repro: rooms too small to keep the run date in the value
        store.add_field(fid, "title", "text", "input", constraints={"maxlength": 14})
        store.add_field(fid, "email", "email", "input", constraints={"maxlength": 25})
        store.add_field(fid, "pw", "password", "input", constraints={"maxlength": 16})
        store.add_field(fid, "site", "url", "input", constraints={"maxlength": 30})
        store.add_field(fid, "age", "number", "input", constraints={"maxlength": 18})
        assert marker_variants(store, m) == {m}  # "app", "appmap-2026", ... never used
        _x(store, "u", "POST", f"{b}/app/settings", status=200, req_body="theme=dark")
        _x(store, "u", "POST", f"{b}/invite", status=201,
           req_body=f"email={m[:12]}%40example.test")
        _x(store, "u", "POST", f"{b}/pw", status=201, req_body=f"pw={m[:11]}-Aa1%21")
        _x(store, "u", "POST", f"{b}/t", status=201, req_body=f"title={m[:14]}")
        assert _record_ids(store, root) == set()
    finally:
        store.close()
