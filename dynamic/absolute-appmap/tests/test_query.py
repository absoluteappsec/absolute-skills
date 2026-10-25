"""Task 20: query recipes and read-only SQL."""
import json
import random
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from appmap.build import build_map
from appmap.cli import main
from appmap.crawl import run_crawl
from appmap.probe.replay import run_active
from appmap.query import (RECIPES, QueryError, format_table, run_recipe, run_sql)
from appmap.rundir import write_run_meta
from appmap.store import Store
from test_replay import seed  # seeds a captured exchange (tests/ is on sys.path)

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}


# -- real fixture run: crawl SSR as admin+alice, probe active, build ------------------
@pytest.fixture(scope="module")
async def ssr_query_run(browser, _ssr_server, tmp_path_factory):
    import os
    import urllib.request

    from appmap.config import load_config
    from appmap.rundir import create_run_dir

    tmp = tmp_path_factory.mktemp("qssr")
    url = _ssr_server.url
    urllib.request.urlopen(urllib.request.Request(url + "/__reset", method="POST", data=b""),
                           timeout=5).read()
    roles = [{"name": n, "login": {"type": "form", "url": f"{url}/login",
                                   "username_env": f"APPMAP_{n.upper()}_USER",
                                   "password_env": f"APPMAP_{n.upper()}_PASS"}}
             for n in ("admin", "alice")]
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    try:
        cfg_path = tmp / "cfg.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "target": {"base_urls": [url]}, "authorization": {"confirmed_by": "test"},
            "profile": "staging", "roles": roles, "limits": FAST}))
        cfg = load_config(cfg_path)
        rd = create_run_dir(cfg, tmp / "runs")
        write_run_meta(rd, cfg, status="done", extra={"config_path": str(cfg_path)})
        await run_crawl(cfg, rd, roles=["admin", "alice"], budget_minutes=5, browser=browser)
        # admin knows /secret-report (never linked from any page); alice never visits it
        seed(cfg, rd, role="admin", method="GET", url=url + "/secret-report",
             resp_body=b"<html><h1>Secret report</h1></html>")
        store = Store(rd.db_path)
        store.upsert_route("/secret-report", url.split("//")[1], "page")
        store.close()
        await run_active(cfg, rd, budget_minutes=5, browser=browser)
        store = Store(rd.db_path)
        store.init_schema()
        build_map(store, rd, [])
        store.close()
        return rd.root
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


@pytest.fixture
def real(ssr_query_run, tmp_path):
    dst = tmp_path / ssr_query_run.name
    shutil.copytree(ssr_query_run, dst)
    store = Store.open_readonly(dst / "appmap.sqlite")
    yield store, dst
    store.close()


def test_hidden_but_reachable(real):
    store, _ = real
    rows = run_recipe(store, "hidden-but-reachable", role="alice")
    items = {r["item"] for r in rows}
    assert "/secret-report" in items and "GET /secret-report" in items
    # things alice reached through her own UI are not hidden
    assert "/projects" not in items and "GET /projects" not in items
    assert all(r["evidence_exchange_id"] for r in rows)
    with pytest.raises(QueryError, match="--role"):
        run_recipe(store, "hidden-but-reachable")


def test_path_to_state_has_locators(tmp_path):
    """B is only reachable by clicking "Next" on A, then submitting the form on B."""
    store = Store(tmp_path / "p.sqlite")
    store.init_schema()
    for sid in ("st-a", "st-b", "st-c"):
        store.upsert_state(sid, "u", f"https://x.test/{sid}", f"/{sid}")
    store.add_transition(None, "st-a", "u", "navigate", "load")
    ls1 = store.add_locator_set("testid")
    store.add_locator(ls1, "css", "a.next", 2)
    store.add_locator(ls1, "testid", "next", 0)
    store.add_transition("st-a", "st-b", "u", "click", "Next", locator_set_id=ls1)
    ls2 = store.add_locator_set("role_name")
    store.add_locator(ls2, "role_name", "button|Save", 0)
    t2 = store.add_transition("st-b", "st-c", "u", "submit", "Save", locator_set_id=ls2)
    rows = run_recipe(store, "path-to-state", state="st-c")
    assert [r["action_type"] for r in rows] == ["goto", "click", "submit"]
    assert rows[0]["url"] == "https://x.test/st-a"
    assert rows[1]["url"] == "https://x.test/st-a" and rows[1]["label"] == "Next"
    assert [l["strategy"] for l in json.loads(rows[1]["locators"])] == ["testid", "css"]
    assert rows[2]["transition_id"] == t2 and rows[2]["url"] == "https://x.test/st-b"
    assert json.loads(rows[2]["locators"])[0]["value"] == "button|Save"
    assert run_recipe(store, "path-to-state", state="st-a")[0]["action_type"] == "goto"
    store.close()


def test_path_to_state_on_real_run(real):
    store, _ = real
    for (sid,) in store.conn.execute("SELECT id FROM states").fetchall():
        rows = run_recipe(store, "path-to-state", state=sid)
        assert rows and rows[0]["action_type"] == "goto" and rows[0]["url"], sid


def test_python_recipes_match_schema_sql_reference(real):
    """path-to-state (BFS) and gaps (compute_gaps) return what the SCHEMA.md SQL returns."""
    store, _ = real
    for (state,) in store.conn.execute("SELECT id FROM states"):
        want = [dict(r) for r in store.conn.execute(RECIPES["path-to-state"], {"state": state})]
        assert run_recipe(store, "path-to-state", state=state) == want, state
    want = [dict(r) for r in store.conn.execute(RECIPES["gaps"])]
    got = run_recipe(store, "gaps")
    assert got == want and got


def test_sql_recipes_run_on_real_db(real):
    store, _ = real
    assert run_recipe(store, "flows")
    assert run_recipe(store, "flows", role="admin")
    assert run_recipe(store, "role-matrix")
    assert run_recipe(store, "forms-posting-to", endpoint="/projects/%")
    shape = run_recipe(store, "endpoint-shape", method="get", path="/projects")
    assert {r["part"] for r in shape} == {"response"} or shape
    fid = run_recipe(store, "flows")[0]["flow_id"]
    run_recipe(store, "flow-form-fields", flow=fid)  # runs; may be empty for non-form flows
    with pytest.raises(QueryError, match="unknown recipe"):
        run_recipe(store, "nope")


def test_cli_query_json_and_table(real, capsys):
    _, root = real
    assert main(["query", "--run", str(root), "hidden-but-reachable", "--role", "alice"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].startswith("APPMAP_STATUS ")
    st = json.loads(lines[-1].removeprefix("APPMAP_STATUS "))
    assert st["ok"] and st["counts"]["rows"] == len(lines) - 1
    assert any(json.loads(ln)["item"] == "/secret-report" for ln in lines[:-1])
    # options may follow the recipe name, in any position
    assert main(["query", "hidden-but-reachable", "--role", "alice", "--run", str(root),
                 "--format", "table"]) == 0
    out = capsys.readouterr().out
    assert "item" in out.splitlines()[0] and "/secret-report" in out
    assert main(["query", "--run", str(root), "sql", "SELECT count(*) AS n FROM states"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["n"] > 0


# -- SQL guard ---------------------------------------------------------------------------
@pytest.fixture
def small(tmp_path):
    db = tmp_path / "appmap.sqlite"
    store = Store(db)
    store.init_schema()
    store.upsert_state("st-a", "u", "https://x.test/", "/")
    store.close()
    store = Store.open_readonly(db)
    yield store
    store.close()


def test_sql_rejects_writes(small):
    for bad in ("DELETE FROM states", "DROP TABLE states", "INSERT INTO meta VALUES ('a','b')",
                "UPDATE states SET role='x'", "PRAGMA journal_mode=DELETE",
                "ATTACH DATABASE ':memory:' AS m", "SELECT 1; DELETE FROM states",
                "SELECT 1; SELECT 2", "WITH x AS (SELECT 1) DELETE FROM states",
                "  -- note\n DELETE FROM states", "/* c */ VACUUM", "", "   "):
        with pytest.raises(QueryError):
            run_sql(small, bad)
    assert small.conn.execute("SELECT count(*) FROM states").fetchone()[0] == 1


def test_sql_allows_reads(small):
    assert run_sql(small, "SELECT id FROM states;") == [{"id": "st-a"}]
    assert run_sql(small, "-- c\n  with x(a) as (select 1) select a from x") == [{"a": 1}]
    assert run_sql(small, "EXPLAIN QUERY PLAN SELECT * FROM states")
    assert run_sql(small, "SELECT json_extract('{\"a\":1}', '$.a') AS v") == [{"v": 1}]
    with pytest.raises(QueryError, match="no such table"):
        run_sql(small, "SELECT * FROM nope")


def test_sql_statement_timeout(small, monkeypatch):
    monkeypatch.setattr("appmap.query.STATEMENT_TIMEOUT_S", 0.3)
    with pytest.raises(QueryError, match="timeout"):
        run_sql(small, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                       "SELECT count(*) FROM c")
    assert run_sql(small, "SELECT 1 AS a") == [{"a": 1}]  # connection usable afterwards


def test_cli_sql_error_status(tmp_path, capsys):
    db = tmp_path / "appmap.sqlite"
    s = Store(db)
    s.init_schema()
    s.close()
    assert main(["query", "--run", str(tmp_path), "sql", "DELETE FROM states"]) == 1
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("APPMAP_STATUS "))
    assert st["command"] == "query" and not st["ok"] and st["errors"]
    assert main(["query", "--run", str(tmp_path / "missing"), "role-matrix"]) == 1
    assert main(["query", "--run", str(tmp_path)]) == 1
    assert main(["query", "role-matrix"]) == 1


def test_format_table_truncates_only_in_table_mode():
    rows = [{"a": "x" * 200, "b": None}]
    t = format_table(rows)
    assert "..." in t and "x" * 200 not in t
    assert "(no rows)" == format_table([])


# -- path-to-state BFS vs SQL on random graphs (cycles, dead ends, ties) ------------------
@pytest.mark.parametrize("seed_", range(8))
def test_path_to_state_matches_sql_on_random_graphs(tmp_path, seed_):
    rnd = random.Random(seed_)
    store = Store(tmp_path / f"g{seed_}.sqlite")
    store.init_schema()
    n = 12
    ids = [f"st-{i:02d}" for i in range(n)]
    for i in ids:
        store.upsert_state(i, "u", f"https://x.test/{i}", f"/{i}")
    for _ in range(30):
        a, b = rnd.choice(ids), rnd.choice(ids)
        kind = rnd.choice(["click", "click", "submit", "navigate"])
        ls = store.add_locator_set("testid")
        store.add_locator(ls, "testid", f"{a}-{b}", 0)
        store.add_transition(a, b, "u", kind, f"{a}->{b}", locator_set_id=ls)
    for _ in range(3):
        store.add_transition(None, rnd.choice(ids), "u", "navigate", "load")
    for sid in ids:
        want = [dict(r) for r in store.conn.execute(RECIPES["path-to-state"], {"state": sid})]
        assert run_recipe(store, "path-to-state", state=sid) == want, sid
    assert run_recipe(store, "path-to-state", state="st-nope") == []
    store.close()



def test_cli_sql_row_cap_and_truncated_count(tmp_path, capsys):
    """M2: query sql prints at most 1000 rows by default; counts.truncated says how many
    were left out; --limit overrides (0 = all)."""
    db = tmp_path / "appmap.sqlite"
    s = Store(db)
    s.init_schema()
    s.close()
    big = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 1205) " \
          "SELECT x FROM c"

    def run(*extra):
        assert main(["query", "--run", str(tmp_path), "sql", big, *extra]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        return out[:-1], json.loads(out[-1].removeprefix("APPMAP_STATUS "))
    rows, st = run()
    assert len(rows) == 1000 and st["counts"]["rows"] == 1000
    assert st["counts"]["truncated"] == 205 and st["next"]
    rows, st = run("--limit", "10")
    assert len(rows) == 10 and st["counts"]["truncated"] == 1195
    rows, st = run("--limit", "0")
    assert len(rows) == 1205 and st["counts"]["truncated"] == 0 and st["next"] is None
