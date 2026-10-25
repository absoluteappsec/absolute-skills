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
