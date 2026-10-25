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


# --- multipart/form-data request bodies -------------------------------------------

_BOUNDARY = "----WebKitFormBoundaryX3sZ9qPq"
_MP_CT = f"multipart/form-data; boundary={_BOUNDARY}"
_FILE_BYTES = b"\x89PNG\r\n\x1a\n\x00\xffavatar-bytes s3cret \x00"


def _mp_body(boundary=_BOUNDARY, nl=b"\r\n"):
    b = boundary.encode()

    def field(name, value):
        return (b"--" + b + nl + b'Content-Disposition: form-data; name="' + name.encode()
                + b'"' + nl + nl + value + nl)
    return (field("csrfmiddlewaretoken", b"Qm9vZ3JhbW1hcjEyMzQ1Njc4OTA")
            + field("username", b"alice") + field("password1", b"Hunter2-pass!")
            + field("ssn", b"123-45-6789") + field("bio", b"my key is s3cret ok")
            + b"--" + b + nl
            + b'Content-Disposition: form-data; name="avatar"; filename="a.png"' + nl
            + b"Content-Type: image/png" + nl + nl + _FILE_BYTES + nl
            + b"--" + b + b"--" + nl)


def _parts(body: bytes, boundary=_BOUNDARY):
    from email.parser import BytesParser
    from email.policy import HTTP
    msg = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n".encode() + body)
    assert not msg.defects
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
            for p in msg.iter_parts()}


def test_redact_multipart_by_field_name():
    from appmap.recording import redact_multipart
    out = redact_multipart(Redactor(["s3cret"], []), _mp_body(), _MP_CT)
    parts = _parts(out)
    assert parts["csrfmiddlewaretoken"] == b"<redacted:csrf>"
    assert parts["password1"] == b"<redacted:password>"
    assert parts["username"] == b"alice"
    assert parts["ssn"] == b"123-45-6789"  # customer data is kept (spec decision 11)
    assert parts["bio"] == b"my key is <redacted:secret> ok"
    # file part bytes preserved except the literal secret
    assert parts["avatar"] == _FILE_BYTES.replace(b"s3cret", b"<redacted:secret>")
    assert b"Hunter2-pass!" not in out and b"Qm9vZ3JhbW1hcjEyMzQ1Njc4OTA" not in out


def test_redact_multipart_quoted_boundary_and_lf_and_str():
    from appmap.recording import redact_multipart, redact_text
    red = Redactor([], [])
    out = redact_multipart(red, _mp_body(nl=b"\n"),
                           f'multipart/form-data; boundary="{_BOUNDARY}"')
    assert b"Hunter2-pass!" not in out and b"<redacted:password>" in out
    assert b"123-45-6789" in out
    text = redact_text(red, _mp_body().decode("latin-1"), _MP_CT)
    assert "Hunter2-pass!" not in text and "alice" in text
    assert _parts(text.encode("latin-1"))["avatar"] == _FILE_BYTES


def test_redact_multipart_without_boundary_falls_back_to_text_scrub():
    from appmap.recording import redact_multipart
    out = redact_multipart(Redactor(["s3cret"], []), b"junk s3cret", "multipart/form-data")
    assert out == b"junk <redacted:secret>"


def test_multipart_request_body_redacted_in_store_and_params(env):
    rec, store, rd, _ = env
    eid = rec(method="POST", url="http://t.test/register/",
              req_headers={"Content-Type": _MP_CT}, req_body=_mp_body(),
              resp_headers={"content-type": "text/html"}, resp_body=b"<p>ok</p>")
    row = rows(store, "SELECT * FROM exchanges WHERE id=?", eid)[0]
    stored = (row["req_body"] or "").encode("latin-1") if row["req_body"] else \
        (rd.root / row["req_body_path"]).read_bytes()
    assert b"Hunter2-pass!" not in stored and b"Qm9vZ3JhbW1hcjEyMzQ1Njc4OTA" not in stored
    parts = _parts(stored)
    assert parts["ssn"] == b"123-45-6789"
    blob = rd.events_path.read_text() + json.dumps(rows(store, "SELECT * FROM params"))
    assert "Hunter2-pass!" not in blob and "Qm9vZ3JhbW1hcjEyMzQ1Njc4OTA" not in blob
    names = {r["name"] for r in rows(store, "SELECT name FROM params WHERE location='body'")}
    assert {"csrfmiddlewaretoken", "username", "password1", "ssn"} <= names
    assert "avatar" not in names


# --- fix round 1: fail-closed multipart parsing -----------------------------------

def _two_parts(closing=b"--B--\r\n", last_tail=b"\r\n"):
    return (b"--B\r\nContent-Disposition: form-data; name=\"user\"\r\n\r\nalice\r\n"
            b"--B\r\nContent-Disposition: form-data; name=\"password\"\r\n\r\nhunter2"
            + last_tail + closing)


@pytest.mark.parametrize("body", [
    _two_parts(closing=b""),                    # missing closing delimiter
    _two_parts(closing=b"", last_tail=b""),     # no trailing CRLF either
    _two_parts(closing=b"--B"),                 # closing delimiter truncated after '--B'
])
def test_multipart_unterminated_last_part_still_redacted(body):
    from appmap.recording import redact_multipart
    out = redact_multipart(Redactor([], []), body, "multipart/form-data; boundary=B")
    assert b"hunter2" not in out and b"<redacted:password>" in out
    assert b"alice" in out


def test_multipart_truncated_last_part_headers():
    from appmap.recording import redact_multipart
    body = (b"--B\r\nContent-Disposition: form-data; name=\"csrfmiddlewaretoken\"\r\n\r\n"
            b"tok-ABCDEFGH-123\r\n--B\r\nContent-Disposition: form-da")
    out = redact_multipart(Redactor([], []), body, "multipart/form-data; boundary=B")
    assert b"tok-ABCDEFGH-123" not in out
    assert out.endswith(b"--B\r\nContent-Disposition: form-da")


def test_multipart_epilogue_kept_after_closing_delimiter():
    from appmap.recording import redact_multipart
    out = redact_multipart(Redactor([], []), _two_parts() + b"epilogue",
                           "multipart/form-data; boundary=B")
    assert out.endswith(b"--B--\r\nepilogue") and b"hunter2" not in out


def test_multipart_json_and_urlencoded_parts_redacted_by_key():
    from appmap.recording import redact_multipart
    body = (b"--B\r\nContent-Disposition: form-data; name=\"meta\"\r\n"
            b"Content-Type: application/json\r\n\r\n{\"api_key\": \"k-123456789\", \"n\": 1}\r\n"
            b"--B\r\nContent-Disposition: form-data; name=\"q\"\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n\r\n"
            b"authenticity_token=zzz-987654321&x=1\r\n--B--\r\n")
    out = redact_multipart(Redactor([], []), body, "multipart/form-data; boundary=B")
    assert b"k-123456789" not in out and b"zzz-987654321" not in out
    assert b"<redacted:api_key>" in out and b"x=1" in out and b'"n": 1' in out


def test_multipart_boundary_sniffed_when_content_type_lacks_it():
    from appmap.recording import redact_multipart
    out = redact_multipart(Redactor([], []), _two_parts(), "multipart/form-data")
    assert b"hunter2" not in out and b"<redacted:password>" in out and b"alice" in out


def test_multipart_non_ascii_literal_secret_in_text_part():
    from appmap.recording import redact_multipart
    secret = "pässwörd-Ünïcode-1"
    body = ("--B\r\nContent-Disposition: form-data; name=\"bio\"\r\n\r\nmine is "
            + secret + "\r\n--B--\r\n").encode("utf-8")
    out = redact_multipart(Redactor([secret], []), body, "multipart/form-data; boundary=B")
    assert secret.encode("utf-8") not in out and b"<redacted:secret>" in out
