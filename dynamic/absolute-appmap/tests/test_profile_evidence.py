"""Profile evidence extraction."""
import json
import os
import time

from appmap.profile.evidence import MAX_EVIDENCE_TEXT, iter_evidence
from appmap.store import Store


def _run(tmp_path):
    root = tmp_path / "appmap-x-20260101-000000"
    (root / "bodies").mkdir(parents=True)
    s = Store(root / "appmap.sqlite")
    s.init_schema()
    return root, s


def test_iter_evidence_kinds(tmp_path):
    root, s = _run(tmp_path)
    s.upsert_role("admin", "form", True)
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/", status=200,
                   content_type="text/html", resp_body="<!doctype html><html></html>",
                   resp_headers={"Server": "gunicorn",
                                 "Set-Cookie": "csrftoken=SECRETCSRF; Path=/\nsessionid=SECRETSESS; HttpOnly"})
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/static/app.js", status=200,
                   content_type="application/javascript", resp_body="console.log(1)")
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/static/app.js.map", status=200,
                   content_type="application/json",
                   resp_body='{"version":3,"sources":["src/App.vue"],"sourcesContent":["<template/>"]}')
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/nope", status=404,
                   content_type="text/html",
                   resp_body="<html>You're seeing this error because you have DEBUG = True</html>")
    s.add_exchange(None, "admin", "passive", method="GET", url="http://x/openapi.json", status=200,
                   content_type="application/json", resp_body='{"openapi":"3.0.0","paths":{}}')
    s.add_exchange(None, "admin", "login", method="POST", url="http://x/login", status=302,
                   req_body="username=bob&password=hunter2",
                   req_headers={"X-CSRF-Token": "tok"})
    s.upsert_route("/app/:id", "x", "spa_route")
    s.close()

    ev = list(iter_evidence(root))
    kinds = {e.kind for e in ev}
    assert kinds == {"header", "cookie", "html", "script", "source_map", "error_page",
                     "schema", "spa", "auth"}
    assert {e.text for e in ev if e.kind == "cookie"} == {"csrftoken", "sessionid"}
    blob = "\n".join(e.text + json.dumps(e.headers) for e in ev)
    for secret in ("SECRETCSRF", "SECRETSESS", "hunter2", "bob"):
        assert secret not in blob
    assert "src/App.vue" in next(e for e in ev if e.kind == "source_map").text
    auth = next(e for e in ev if e.kind == "auth")
    assert "username,password" in auth.text and "x-csrf-token" in auth.text
    assert all(e.run_id == "20260101-000000" for e in ev)


def test_body_cap(tmp_path):
    root, s = _run(tmp_path)
    big = root / "bodies" / "1.resp"
    with open(big, "wb") as fh:
        fh.write(b"a" * (1024 * 1024) * 50)
    s.add_exchange(None, "r", "ui", method="GET", url="http://x/big.js", status=200,
                   content_type="application/javascript", resp_body_path="bodies/1.resp")
    s.close()
    t = time.monotonic()
    ev = [e for e in iter_evidence(root) if e.kind == "script"]
    assert time.monotonic() - t < 2
    assert len(ev) == 1 and len(ev[0].text) <= MAX_EVIDENCE_TEXT


def test_readonly(tmp_path):
    root, s = _run(tmp_path)
    s.add_exchange(None, "r", "ui", method="GET", url="http://x/", status=200,
                   content_type="text/html", resp_body="<html></html>")
    s.close()
    db = root / "appmap.sqlite"
    before = os.stat(db).st_mtime_ns
    list(iter_evidence(root))
    assert os.stat(db).st_mtime_ns == before
