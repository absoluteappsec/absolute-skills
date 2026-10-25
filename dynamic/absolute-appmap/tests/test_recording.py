import json

import pytest

from appmap.events import EventLog
from appmap.recording import detect_observations, record_exchange
from appmap.redact import Redactor
from appmap.rundir import create_run_dir
from appmap.store import Store


@pytest.fixture
def env(cfg, tmp_path):
    rd = create_run_dir(cfg, tmp_path)
    red = Redactor(["s3cret"], [])
    store = Store(rd.db_path)
    store.init_schema()
    ev = EventLog(rd.events_path, red)

    def rec(**kw):
        base = dict(role="admin", origin="ui", method="GET", url="http://t.test/x",
                    req_headers={}, req_body=None, status=200, resp_headers={},
                    resp_body=None, duration_ms=1.0, caused_by=None, known_patterns=[])
        base.update(kw)
        return record_exchange(store, ev, red, rd, **base)

    yield rec, store, rd, ev
    store.close()


def rows(store, sql, *a):
    return [dict(r) for r in store.conn.execute(sql, a)]


def test_graphql_keyed_by_operation(env):
    rec, store, _, _ = env
    for q in ("query ListItems { items { id } }", "mutation DeleteItem($id: ID!) { del(id:$id) }"):
        rec(method="POST", url="http://t.test/graphql",
            req_headers={"content-type": "application/json"},
            req_body=json.dumps({"query": q}).encode(),
            resp_headers={"content-type": "application/json"}, resp_body=b'{"data":{}}')
    eps = rows(store, "SELECT * FROM endpoints ORDER BY id")
    assert [(e["graphql_op"], e["graphql_type"], e["kind"]) for e in eps] == [
        ("ListItems", "query", "graphql_op"), ("DeleteItem", "mutation", "graphql_op")]


def test_graphql_operation_name_fallback(env):
    rec, store, _, _ = env
    rec(method="POST", url="http://t.test/graphql", req_body=json.dumps(
        {"query": "{ me { id } }", "operationName": "Me"}).encode(),
        req_headers={"content-type": "application/json"})
    assert rows(store, "SELECT graphql_op FROM endpoints")[0]["graphql_op"] == "Me"


def test_large_body_goes_to_file(env):
    rec, store, rd, _ = env
    big = b"a" * (262144 + 1)
    eid = rec(resp_headers={"content-type": "text/plain"}, resp_body=big)
    row = rows(store, "SELECT * FROM exchanges WHERE id=?", eid)[0]
    assert row["resp_body"] is None
    assert row["resp_body_path"].startswith(f"bodies/{eid}.")
    assert (rd.root / row["resp_body_path"]).read_bytes() == big
    small = rec(resp_headers={"content-type": "text/plain"}, resp_body=b"hi")
    assert rows(store, "SELECT resp_body FROM exchanges WHERE id=?", small)[0]["resp_body"] == "hi"


def test_binary_body_always_file(env):
    rec, store, rd, _ = env
    eid = rec(resp_headers={"content-type": "image/png"}, resp_body=b"\x89PNG\r\n")
    row = rows(store, "SELECT * FROM exchanges WHERE id=?", eid)[0]
    assert row["resp_body"] is None and row["resp_body_path"].endswith(".png")
    assert (rd.root / row["resp_body_path"]).read_bytes() == b"\x89PNG\r\n"


def test_body_shape_skeleton(env):
    rec, store, _, _ = env
    rec(url="http://t.test/api/items", resp_headers={"content-type": "application/json"},
        resp_body=b'{"items":[{"id":1,"name":"a"}],"next":null}')
    shape = json.loads(rows(store, "SELECT body_shape FROM responses")[0]["body_shape"])
    assert shape == {"items": [{"id": "int", "name": "str"}], "next": "null"}


def test_observations_detected(env):
    rec, store, _, _ = env
    rec(status=500, resp_headers={"content-type": "text/plain"},
        resp_body=b"Traceback (most recent call last):\n  File x")
    kinds = {r["kind"] for r in rows(store, "SELECT kind FROM observations")}
    assert {"server_error", "stack_trace"} <= kinds


def test_detect_observations_kinds():
    k = lambda *a: {x for x, _ in detect_observations(*a)}
    assert k(429, None, None, {}) == {"rate_limited"}
    assert "rate_limited" in k(503, None, None, {"Retry-After": "5"})
    assert "debug_page" in k(500, "<title>Werkzeug Debugger</title>", "text/html", {})
    assert "captcha" in k(200, '<div class="g-recaptcha"></div>', "text/html", {})
    assert "captcha" in k(200, "challenges.cloudflare.com/turnstile", "text/html", {})
    assert "stack_trace" in k(200, "\tat com.x.Foo.bar(Foo.java:42)", "text/plain", {})
    assert k(200, "<html>fine</html>", "text/html", {}) == set()


def test_endpoint_params_and_path_normalization(env):
    rec, store, _, _ = env
    rec(url="http://t.test/api/items/42?q=a&token=s3cret", method="POST",
        req_headers={"content-type": "application/json"}, req_body=b'{"name":"x","n":1}')
    rec(url="http://t.test/api/items/43?q=b", method="POST",
        req_headers={"content-type": "application/json"}, req_body=b'{"name":"y"}')
    ep = rows(store, "SELECT * FROM endpoints")
    assert len(ep) == 1 and ep[0]["path_pattern"] == "/api/items/{id}"
    p = {(r["location"], r["name"]): r for r in rows(store, "SELECT * FROM params")}
    assert json.loads(p[("path", "id")]["examples"]) == ["42", "43"]
    assert json.loads(p[("query", "q")]["examples"]) == ["a", "b"]
    assert "s3cret" not in json.dumps([dict(r) for r in p.values()])
    assert ("body", "name") in p and ("body", "n") in p
    assert rows(store, "SELECT source FROM sources")[0]["source"] == "ui"


def test_responses_merge_statuses_and_kinds(env, ):
    rec, store, _, _ = env
    rec(url="http://t.test/a.js", resp_headers={"content-type": "text/javascript"},
        resp_body=b"x")
    rec(url="http://t.test/a.js", status=404, resp_body=b"nope")
    rec(url="http://t.test/home", resp_headers={"content-type": "text/html; charset=utf-8"},
        resp_body=b"<html></html>")
    rec(url="http://t.test/stream", resp_headers={"content-type": "text/event-stream"})
    kinds = {r["path_pattern"]: r["kind"] for r in rows(store, "SELECT * FROM endpoints")}
    assert kinds == {"/a.js": "static", "/home": "page", "/stream": "sse"}
    r = rows(store, "SELECT * FROM responses r JOIN endpoints e ON e.id=r.endpoint_id "
                    "WHERE path_pattern='/a.js'")[0]
    assert json.loads(r["statuses"]) == [200, 404]
    assert len(json.loads(r["sample_exchange_ids"])) == 2


def test_secrets_redacted_everywhere(env):
    rec, store, rd, _ = env
    rec(url="http://t.test/x?api_key=s3cret", req_headers={"Authorization": "Bearer s3cret"},
        req_body=b'{"password":"s3cret"}', resp_headers={"content-type": "application/json",
        "Set-Cookie": "sid=abc; Path=/"}, resp_body=b'{"token":"s3cret"}')
    blob = json.dumps(rows(store, "SELECT * FROM exchanges")) + rd.events_path.read_text()
    assert "s3cret" not in blob and "sid=abc" not in blob
    types = [json.loads(l)["type"] for l in rd.events_path.read_text().splitlines()]
    assert types == ["request", "response"]


def test_binary_and_oversized_bodies_redacted_in_files(env):
    rec, store, rd, _ = env
    e1 = rec(resp_headers={"content-type": "application/octet-stream"},
             resp_body=b"\x00\xffhead s3cret tail\x80")
    e2 = rec(resp_headers={"content-type": "text/plain"},
             resp_body=b"s3cret " + b"a" * 262144)
    for eid in (e1, e2):
        path = rows(store, "SELECT resp_body_path p FROM exchanges WHERE id=?", eid)[0]["p"]
        assert b"s3cret" not in (rd.root / path).read_bytes()
    assert (rd.root / rows(store, "SELECT resp_body_path p FROM exchanges WHERE id=?", e1)[0]["p"]
            ).read_bytes().startswith(b"\x00\xffhead ")


def test_endpoint_kind_never_downgraded(env):
    rec, store, _, _ = env
    rec(url="http://t.test/api/x", resp_headers={"content-type": "application/json"}, resp_body=b"{}")
    rec(url="http://t.test/api/x", status=500, resp_headers={"content-type": "text/html"},
        resp_body=b"<html>err</html>")
    assert rows(store, "SELECT kind FROM endpoints")[0]["kind"] == "rest"


def test_body_shape_keeps_first_2xx(env):
    rec, store, _, _ = env
    js = {"content-type": "application/json"}
    shape = lambda: json.loads(rows(store, "SELECT body_shape FROM responses")[0]["body_shape"])
    rec(url="http://t.test/api/y", status=500, resp_headers=js, resp_body=b'{"error":"x"}')
    assert shape() == {"error": "str"}
    rec(url="http://t.test/api/y", resp_headers=js, resp_body=b'{"id":1}')
    assert shape() == {"id": "int"}
    rec(url="http://t.test/api/y", status=404, resp_headers=js, resp_body=b'{"error":"x"}')
    rec(url="http://t.test/api/y", resp_headers=js, resp_body=b'{"other":1}')
    assert shape() == {"id": "int"}


def test_source_explicit_default_and_unknown(env):
    rec, store, _, _ = env
    rec(url="http://t.test/robots.txt", origin="passive", source="robots")
    with pytest.raises(ValueError):
        rec(url="http://t.test/z", origin="passive")
    with pytest.raises(ValueError):
        rec(url="http://t.test/z", origin="bogus")
    rec(url="http://t.test/graphql", origin="schema", method="POST",
        req_headers={"content-type": "application/json"},
        req_body=b'{"query":"query Q { a }"}')
    rec(url="http://t.test/openapi.json", origin="schema")
    assert sorted(r["source"] for r in rows(store, "SELECT source FROM sources")) == [
        "graphql", "openapi", "robots"]


def test_form_urlencoded_request_body_redacted_by_name():
    """I10: a CSRF token posted in a form body is redacted by its parameter name."""
    from appmap.recording import redact_text
    from appmap.redact import Redactor
    out = redact_text(Redactor([], []), "authenticity_token=wDlCmkO01QF_Dg&name=appmap-1",
                      "application/x-www-form-urlencoded")
    assert "wDlCmkO01QF_Dg" not in out and "name=appmap-1" in out
