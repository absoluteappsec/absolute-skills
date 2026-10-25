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
