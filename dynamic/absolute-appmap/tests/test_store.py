import sqlite3

import pytest

from appmap.store import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "appmap.db")
    s.init_schema()
    yield s
    s.close()


def test_schema_version_recorded(store):
    assert SCHEMA_VERSION == "1.0.0"
    assert store.get_meta("schema_version") == "1.0.0"
    assert store.get_meta("missing") is None
    store.set_meta("k", "v")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"


def test_wal_and_row_factory(store):
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("SELECT 1 AS a").fetchone()["a"] == 1


def test_upsert_endpoint_idempotent(store):
    a = store.upsert_endpoint("GET", "h", "/api/x/{id}", kind="rest", content_types=["application/json"])
    b = store.upsert_endpoint("GET", "h", "/api/x/{id}", kind="rest", content_types=["text/plain"])
    assert a == b
    row = store.conn.execute("SELECT * FROM endpoints WHERE id=?", (a,)).fetchone()
    assert row["content_types"] == '["text/plain"]'
    c = store.upsert_endpoint("POST", "h", "/graphql", kind="graphql_op", graphql_op="Q1")
    d = store.upsert_endpoint("POST", "h", "/graphql", kind="graphql_op", graphql_op="Q1")
    e = store.upsert_endpoint("POST", "h", "/graphql", kind="graphql_op", graphql_op="Q2")
    assert c == d != e


def test_role_access_rejects_bad_outcome(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_role_access("route", 1, "admin", "bogus")
    i = store.upsert_role_access("route", 1, "admin", "allowed")
    assert store.upsert_role_access("route", 1, "admin", "forbidden") == i


def test_readonly_store_rejects_writes(tmp_path):
    p = tmp_path / "appmap.db"
    w = Store(p)
    w.init_schema()
    w.close()
    ro = Store.open_readonly(p)
    assert ro.get_meta("schema_version") == "1.0.0"
    with pytest.raises(sqlite3.OperationalError):
        ro.set_meta("x", "y")
    ro.close()


def test_dao_roundtrip_all_tables(store):
    store.upsert_role("admin", login_type="form", auth_ok=True, last_login_event="ev-000001")
    sid = store.upsert_state("s1", "admin", "http://h/", route_pattern="/", dom_fingerprint="f")
    assert sid == "s1"
    store.upsert_state("s1", "admin", "http://h/", route_pattern="/", dom_fingerprint="f")
    assert store.conn.execute("SELECT visits FROM states").fetchone()[0] == 2
    ls = store.add_locator_set("testid")
    store.add_locator(ls, "testid", "go", 0)
    t = store.add_transition("s1", "s2", "admin", "click", "Go", ls, "ev-2", exchange_ids=[1])
    r = store.upsert_route("/", "h", "page")
    assert store.upsert_route("/", "h", "page") == r
    ep = store.upsert_endpoint("GET", "h", "/a", kind="rest")
    p = store.upsert_param(ep, "query", "q", types=["string"], examples=["a"])
    assert store.upsert_param(ep, "query", "q", types=["int"]) == p
    ex = store.add_exchange(ep, "admin", "ui", method="GET", url="http://h/a", status=200,
                            req_headers={"a": "b"})
    rs = store.upsert_response(ep, "admin", statuses=[200], sample_exchange_ids=[ex])
    assert store.upsert_response(ep, "admin", statuses=[200, 404]) == rs
    f = store.add_form("s1", inferred=False, method="POST", action_endpoint_id=ep)
    store.add_field(f, "name", type="text", constraints={"max": 12})
    fl = store.add_flow("create", "admin", "d", preconditions=[])
    store.add_flow_step(fl, 0, transition_id=t, exchange_id=ex)
    store.add_value_link(ex, "$.id", ex, "path", "abc")
    store.upsert_source("endpoint", ep, "ui")
    assert store.upsert_source("endpoint", ep, "ui") == store.upsert_source("endpoint", ep, "ui")
    store.upsert_approval("a1", "admin", "s1", "delete", "Delete", reasons=["x"], action_path=[],
                          predicted_request={"m": "DELETE"})
    store.upsert_approval("a1", "admin", "s1", "delete", "Delete", decision="approve", decided_by="me")
    row = store.conn.execute("SELECT decision, decided_by FROM approvals").fetchone()
    assert (row[0], row[1]) == ("approve", "me")
    store.add_observation("note", "d", url="http://h/", role="admin", event_id="ev-1")
    e1 = store.upsert_external_link("http://x", "http://h/")
    assert store.upsert_external_link("http://x", "http://h/") == e1


def test_frontier_defaults_dedup(store):
    cols = {r["name"]: r for r in store.conn.execute("PRAGMA table_info(frontier)")}
    assert cols["url"]["notnull"] == 1 and cols["url"]["dflt_value"] == "''"
    assert cols["action_path"]["notnull"] == 1 and cols["action_path"]["dflt_value"] == "'[]'"
    q = "INSERT INTO frontier(role,kind,depth,priority,status) VALUES('a','url',0,0,'pending')"
    store.conn.execute(q)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(q)


def test_readonly_path_with_special_chars(tmp_path):
    d = tmp_path / "a#b %20c?x"
    d.mkdir()
    p = d / "appmap.db"
    w = Store(p)
    w.init_schema()
    w.set_meta("marker", "here")
    w.close()
    ro = Store.open_readonly(p)
    assert ro.get_meta("marker") == "here"
    ro.close()


def test_init_schema_version_compat(tmp_path):
    p = tmp_path / "appmap.db"
    s = Store(p)
    s.init_schema()
    s.set_meta("schema_version", "1.0.5")
    s.init_schema()  # same major: left alone
    assert s.get_meta("schema_version") == "1.0.5"
    s.set_meta("schema_version", "2.0.0")
    with pytest.raises(RuntimeError, match="incompatible"):
        s.init_schema()
    assert s.get_meta("schema_version") == "2.0.0"
    s.close()
