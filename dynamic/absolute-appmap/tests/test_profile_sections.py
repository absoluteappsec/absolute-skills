"""Profile sections I: layout, data model, environment."""
import json

from appmap.profile.sections import data_model, environment, layout
from appmap.rundir import open_run_dir
from appmap.store import Store


def _open(run):
    rd = open_run_dir(run)
    return Store.open_readonly(rd.db_path), rd.run_id


def test_layout_ssr(ssr_built_run):
    store, run_id = _open(ssr_built_run)
    try:
        out = layout(store, run_id)
    finally:
        store.close()
    prefixes = {a["prefix"] for a in out["areas"]}
    assert {"/projects", "/admin", "/api"} <= prefixes
    admin = next(a for a in out["areas"] if a["prefix"] == "/admin")
    assert "admin" in admin["roles"] and "/admin" in admin["routes"]
    api = next(a for a in out["areas"] if a["prefix"] == "/api")
    assert "GET /api/projects" in api["endpoints"]
    assert "projects" in {g["resource"] for g in out["api_groups"]}
    assert out["entry_points"]["admin"] and set(out["entry_points"]) >= {"admin", "alice"}


def test_data_model_ssr_inferred(ssr_built_run):
    store, run_id = _open(ssr_built_run)
    try:
        ents = data_model(store, run_id)
    finally:
        store.close()
    ent = next(e for e in ents if e["name"] == "projects")
    assert ent["source"] == "inferred" and ent["id_format"] == "int"
    assert {"id", "name"} <= {f["name"] for f in ent["fields"]}
    assert any(e.endswith("/api/projects/{id}") for e in ent["endpoints"])
    assert ent["evidence"] and all(r["run_id"] == run_id for r in ent["evidence"])


def test_data_model_openapi_stub(tmp_path):
    root = tmp_path / "appmap-x-20260101-000000"
    (root / "bodies").mkdir(parents=True)
    s = Store(root / "appmap.sqlite")
    s.init_schema()
    spec = {"openapi": "3.0.0", "paths": {"/api/users": {"get": {"responses": {"200": {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}}}}},
        "components": {"schemas": {"User": {
            "type": "object", "required": ["id", "email"],
            "properties": {"id": {"type": "string", "format": "uuid"},
                           "email": {"type": "string", "format": "email", "maxLength": 80},
                           "role": {"type": "string", "enum": ["a", "b"]},
                           "age": {"type": "integer", "minimum": 0}}}}}}
    s.add_exchange(None, "admin", "schema", method="GET", url="http://x/openapi.json",
                   status=200, content_type="application/json", resp_body=json.dumps(spec))
    s.close()
    store = Store.open_readonly(root / "appmap.sqlite")
    ents = data_model(store, "20260101-000000")
    store.close()
    user = next(e for e in ents if e["name"] == "User")
    assert user["source"] == "openapi" and user["id_format"] == "uuid"
    f = {x["name"]: x for x in user["fields"]}
    assert f["email"]["type"] == "string" and f["email"]["required"] is True
    assert f["email"]["constraints"] == {"format": "email", "maxLength": 80}
    assert f["role"]["enum"] == ["a", "b"] and f["role"]["required"] is False
    assert f["age"]["type"] == "integer"
    assert user["endpoints"] == ["GET /api/users"]
    assert user["evidence"] == [{"run_id": "20260101-000000", "kind": "exchange", "id": 1}]


def test_environment_matches_run_json(ssr_built_run):
    meta = json.loads((ssr_built_run / "run.json").read_text())
    env = environment(ssr_built_run)
    assert env["run_id"] == meta["run_id"] and env["profile"] == meta["profile"]
    assert env["started_at"] == meta["started_at"]
    assert env["tool_version"] == meta["tool_version"]
    assert env["contains_production_data"] is False
    assert env["base_urls"] == meta["config"]["base_urls"]
    assert env["hosts"] == ["127.0.0.1:" + env["base_urls"][0].rsplit(":", 1)[1]]


def _stub_store(tmp_path):
    root = tmp_path / "appmap-x-20260101-000000"
    (root / "bodies").mkdir(parents=True)
    s = Store(root / "appmap.sqlite")
    s.init_schema()
    return root, s


def _model(root):
    store = Store.open_readonly(root / "appmap.sqlite")
    try:
        return data_model(store, "20260101-000000")
    finally:
        store.close()


def test_data_model_spa_items_entity(spa_built_run):
    store, run_id = _open(spa_built_run)
    try:
        ents = data_model(store, run_id)
        groups = layout(store, run_id)["api_groups"]
    finally:
        store.close()
    names = {e["name"] for e in ents}
    assert not names & {"admin", "graphql", "internal", "login", "openapi.json", "api"}
    items = next(e for e in ents if e["name"] == "items")
    got = {f["name"]: f for f in items["fields"]}
    assert {"id", "title", "owner"} <= set(got)
    assert not set(got) & {"items", "next_cursor", "Title", "combo"}
    assert got["id"]["type"] == "integer" and items["id_format"] == "int"
    assert not any("." in g["resource"] for g in groups)


def test_data_model_redacts_and_skips_sensitive(tmp_path):
    root, s = _stub_store(tmp_path)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJlc2ln"
    spec = {"openapi": "3.0.0", "paths": {}, "components": {"schemas": {"Acct": {
        "type": "object", "properties": {
            "note": {"type": "string", "example": jwt},
            "api_key": {"type": "string", "example": "sk-live-123456"},
            "email": {"type": "string", "example": "bob@example.com"}}}}}}
    s.add_exchange(None, "a", "schema", method="GET", url="http://x/openapi.json", status=200,
                   content_type="application/json", resp_body=json.dumps(spec))
    ep = s.upsert_endpoint("GET", "x", "/api/things/{id}", "rest")
    coll = s.upsert_endpoint("POST", "x", "/api/things", "rest")
    s.upsert_param(ep, "path", "id", ["str"], ["7"])
    s.upsert_param(coll, "body", "name", ["str"], ["widget"])
    s.upsert_param(coll, "body", "password", ["str"], ["hunter2"])
    s.upsert_param(coll, "body", "api_key", ["str"], ["sk-live-123456"])
    s.close()
    ents = _model(root)
    blob = json.dumps(ents)
    for secret in (jwt, "sk-live-123456", "hunter2", "bob@example.com"):
        assert secret not in blob
    thing = next(e for e in ents if e["name"] == "things")
    assert [f["name"] for f in thing["fields"]] == ["name"]
    acct = next(e for e in ents if e["name"] == "Acct")
    assert all(f["example_redacted"] in (None,) or "eyJ" not in f["example_redacted"]
               for f in acct["fields"])


def test_data_model_graphql_introspection(tmp_path):
    root, s = _stub_store(tmp_path)
    nn = lambda t: {"kind": "NON_NULL", "name": None, "ofType": t}  # noqa: E731
    scalar = lambda n: {"kind": "SCALAR", "name": n, "ofType": None}  # noqa: E731
    result = {"data": {"__schema": {
        "queryType": {"name": "Query"}, "mutationType": None, "subscriptionType": None,
        "types": [
            {"name": "Query", "kind": "OBJECT", "fields": [{"name": "items", "type": scalar("X")}]},
            {"name": "__Type", "kind": "OBJECT", "fields": []},
            {"name": "Item", "kind": "OBJECT", "fields": [
                {"name": "id", "type": nn(scalar("ID"))},
                {"name": "title", "type": scalar("String")},
                {"name": "tags", "type": {"kind": "LIST", "name": None, "ofType": scalar("String")}}]},
        ]}}}
    s.add_exchange(None, "a", "schema", method="POST", url="http://x/graphql", status=200,
                   content_type="application/json", resp_body=json.dumps(result))
    s.close()
    ents = _model(root)
    assert [e["name"] for e in ents] == ["Item"]
    item = ents[0]
    f = {x["name"]: x for x in item["fields"]}
    assert item["source"] == "graphql" and item["id_format"] == "unknown"
    assert f["id"]["required"] is True and f["id"]["type"] == "ID"
    assert f["title"]["required"] is False and f["tags"]["type"] == "list<String>"


def test_data_model_dedupes_schema_entities_and_bare_id(tmp_path):
    root, s = _stub_store(tmp_path)
    for url, props in (("http://x/openapi.json", {"id": {"type": "integer"}}),
                       ("http://x/api/openapi.json", {"id": {"type": "integer"},
                                                      "name": {"type": "string"}})):
        spec = {"openapi": "3.0.0", "paths": {}, "components": {"schemas": {
            "Project": {"type": "object", "properties": props}}}}
        s.add_exchange(None, "a", "schema", method="GET", url=url, status=200,
                       content_type="application/json", resp_body=json.dumps(spec))
    ep = s.upsert_endpoint("GET", "x", "/api/things/{id}", "rest")
    s.upsert_response(ep, "a", [200], ["application/json"], {"id": "int", "n": "str"})
    s.upsert_param(ep, "path", "id")  # no type, no example
    s.close()
    ents = _model(root)
    proj = [e for e in ents if e["name"] == "Project"]
    assert len(proj) == 1
    assert [f["name"] for f in proj[0]["fields"]] == ["id", "name"]
    assert [r["id"] for r in proj[0]["evidence"]] == [1, 2]
    assert next(e for e in ents if e["name"] == "things")["id_format"] == "unknown"


# ----------------------------------------------------------------- sections II
import pytest  # noqa: E402

from appmap.profile.sections import (accounts, coverage, data_setup, features,  # noqa: E402
                                     mask_identity)


@pytest.mark.parametrize("raw,masked", [
    ("chris@example.com", "c***@example.com"),
    ("chris", "c***"),
    ("a@b.io", "a***@b.io"),
    ("", ""),
])
def test_mask_identity(raw, masked):
    assert mask_identity(raw) == masked


def _with_store(run, fn):
    store, run_id = _open(run)
    try:
        return fn(store, run_id), run_id
    finally:
        store.close()


def test_features_ssr(ssr_built_run):
    feats, run_id = _with_store(ssr_built_run, features)
    by = {f["resource"]: f for f in feats}
    assert {"projects", "admin"} <= set(by)
    proj = by["projects"]
    assert "admin|POST /projects/new" in proj["flow_keys"]
    assert "/projects/new" in proj["routes"] and "GET /api/projects" in proj["endpoints"]
    assert proj["forms"] and {"admin", "alice"} <= set(proj["roles"])
    assert proj["name_suggestion"] == "Projects"
    import hashlib
    want = "ft-" + hashlib.sha1(
        ("projects|" + "|".join(sorted(proj["flow_keys"]))).encode()).hexdigest()[:12]
    assert proj["key"] == want
    assert by["admin"]["roles"] == ["admin"]
    assert len({f["key"] for f in feats}) == len(feats)


def test_data_setup_ssr(ssr_built_run):
    out, run_id = _with_store(ssr_built_run, data_setup)
    pre = {p["flow_key"]: p for p in out["preconditions"]}
    assert "admin|POST /projects/new" in pre and pre["admin|POST /projects/new"]["requires"]
    assert all(p["requires"] for p in out["preconditions"])
    recs = out["created_records"]
    assert recs and any(r["method"] == "POST" and r["endpoint"] == "/projects/new" for r in recs)
    assert all(r["ref"] == {"run_id": run_id, "kind": "exchange", "id": r["exchange_id"]}
               for r in recs)
    assert out["fixtures_used"] is False
    seed = {s["resource"]: s for s in out["seed_observations"]}
    assert seed["projects"]["id_min"] == 1 and seed["projects"]["count"] >= 1
    assert seed["projects"]["id_max"] >= seed["projects"]["id_min"]


def test_data_setup_spa_seed_unwraps_wrapper(spa_built_run):
    out, _ = _with_store(spa_built_run, data_setup)
    seed = {s["resource"]: s for s in out["seed_observations"]}
    assert seed["items"]["id_min"] == 1 and seed["items"]["count"] >= 10


def test_accounts_ssr(ssr_built_run):
    out, run_id = _with_store(ssr_built_run, lambda s, r: accounts(s, r, mask=False))
    roles = {r["name"]: r for r in out["roles"]}
    assert set(roles) == {"admin", "alice", "anonymous"}
    assert roles["admin"]["login_type"] == "form" and roles["anonymous"]["login_type"] == "none"
    assert roles["admin"]["reachable_routes"] > roles["alice"]["reachable_routes"]
    assert roles["admin"]["reachable_endpoints"] >= 1
    assert "password" not in json.dumps(out["roles"]).lower()
    assert len(out["observed"]) <= 50


def test_accounts_observed_and_masking(tmp_path):
    root = tmp_path / "appmap-x-20260101-000000"
    (root / "bodies").mkdir(parents=True)
    s = Store(root / "appmap.sqlite")
    s.init_schema()
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/api/me", status=200,
                   content_type="application/json",
                   resp_body=json.dumps({"username": "chris", "email": "chris@example.com",
                                         "password": "hunter2", "token": "abc123",
                                         "login": True}))
    s.add_exchange(None, "admin", "ui", method="GET", url="http://x/contact", status=200,
                   content_type="text/html",
                   resp_body='<a href="mailto:help@corp.example?subject=x">mail</a>')
    s.close()
    store = Store.open_readonly(root / "appmap.sqlite")
    plain = accounts(store, "20260101-000000", mask=False)
    masked = accounts(store, "20260101-000000", mask=True)
    store.close()
    vals = {o["value"] for o in plain["observed"]}
    assert vals == {"chris", "chris@example.com", "help@corp.example"}
    assert {o["value"] for o in masked["observed"]} == {"c***", "c***@example.com",
                                                        "h***@corp.example"}
    assert all(o["where"]["kind"] == "exchange" for o in plain["observed"])
    blob = json.dumps(plain)
    assert "hunter2" not in blob and "abc123" not in blob


def test_coverage_ssr(ssr_built_run):
    out, _ = _with_store(ssr_built_run, coverage)
    feats, _ = _with_store(ssr_built_run, features)
    admin = next(f for f in feats if f["resource"] == "admin")
    grid = out["features_x_roles"]
    assert grid[admin["key"]]["admin"] == "allowed"
    assert grid[admin["key"]]["alice"] == "forbidden"
    assert set(grid) == {f["key"] for f in feats}
    assert set(out["unexercised"]) >= {"unexercised_endpoints", "unreached_routes"}
    assert out["approvals"]["pending"] >= 1 and out["approvals"]["denied"] >= 1
    assert set(out["approvals"]) == {"pending", "denied", "skipped"}


# ------------------------------------------------------------ fix round 1
def _stub_run(tmp_path, exchanges, run_id="20260101-000000"):
    root = tmp_path / f"appmap-x-{run_id}"
    (root / "bodies").mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"run_id": run_id, "config": {}}))
    s = Store(root / "appmap.sqlite")
    s.init_schema()
    for kw in exchanges:
        s.add_exchange(None, "admin", "ui", **kw)
    s.close()
    return Store.open_readonly(root / "appmap.sqlite"), run_id


def test_accounts_created_by_run_marker_and_signup(tmp_path):
    m = "appmap-20260101-000000"
    store, rid = _stub_run(tmp_path, [
        dict(method="POST", url="http://x/register", status=302,
             req_headers={"content-type": "application/x-www-form-urlencoded"},
             req_body=f"username={m}-u&email={m}%40example.com&password=Zz9!"),
        dict(method="POST", url="http://x/api/v1/members", status=201,
             req_headers={"content-type": "application/json"},
             req_body=json.dumps({"email": f"{m}@corp.example", "role": "x"})),
        dict(method="POST", url="http://x/signup", status=302,
             req_body="username=realperson&email=real.person%40corp.example"),
        dict(method="POST", url="http://x/api/widgets", status=201,
             req_body=json.dumps({"name": "w"})),
    ])
    plain = accounts(store, rid, mask=False)["created_by_run"]
    masked = accounts(store, rid, mask=True)["created_by_run"]
    store.close()
    by = {c["endpoint"]: c for c in masked}
    assert set(by) == {"/register", "/api/v1/members", "/signup"}
    assert by["/register"]["username"] == f"{m}-u"            # run-generated: unmasked
    assert by["/register"]["email"] == f"{m}@example.com"
    assert by["/api/v1/members"]["email"] == f"{m}@corp.example"
    assert by["/api/v1/members"]["username"] is None
    assert by["/signup"]["username"] == "r***" and by["/signup"]["email"] == "r***@corp.example"
    assert {c["endpoint"]: c for c in plain}["/signup"]["username"] == "realperson"
    assert "Zz9" not in json.dumps(plain)


def test_observed_rejects_token_like_values(tmp_path):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    store, rid = _stub_run(tmp_path, [dict(
        method="GET", url="http://x/api/me", status=200, content_type="application/json",
        resp_body=json.dumps({"user": jwt, "login": "deadbeefcafe1234abcd",
                              "username": "bcdfghjklmnpq", "email": "ok@corp.example",
                              "x": {"user": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"}}))])
    out = accounts(store, rid, mask=False)["observed"]
    store.close()
    assert [o["value"] for o in out] == ["ok@corp.example"]


def test_observed_marker_identity_unmasked(tmp_path):
    m = "appmap-20260101-000000"
    store, rid = _stub_run(tmp_path, [dict(
        method="GET", url="http://x/api/users", status=200, content_type="application/json",
        resp_body=json.dumps([{"username": f"{m}-u"}, {"username": "chris"}]))])
    vals = {o["value"] for o in accounts(store, rid, mask=True)["observed"]}
    store.close()
    assert vals == {f"{m}-u", "c***"}


def test_mailto_percent_decoded(tmp_path):
    store, rid = _stub_run(tmp_path, [dict(
        method="GET", url="http://x/c", status=200, content_type="text/html",
        resp_body='<a href="mailto:a%2Bb%40corp.example">m</a>')])
    vals = [o["value"] for o in accounts(store, rid, mask=False)["observed"]]
    store.close()
    assert vals == ["a+b@corp.example"]


def test_created_records_capped(tmp_path):
    store, rid = _stub_run(tmp_path, [
        dict(method="POST", url=f"http://x/api/things/{i}", status=201) for i in range(120)])
    out = data_setup(store, rid)
    store.close()
    assert len(out["created_records"]) == 100 and out["created_records_more"] == 20
