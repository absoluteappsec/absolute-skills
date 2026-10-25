import functools

import pytest
from playwright.async_api import async_playwright

from appmap.direct import DirectClient
from appmap.events import EventLog
from appmap.frontier import Frontier
from appmap.probe.schemas import SchemaOp, parse_introspection, parse_openapi, probe_schemas
from appmap.recording import record_exchange
from appmap.redact import Redactor


def test_parse_openapi_v3_params():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items/{id}": {
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "get": {"parameters": [{"name": "verbose", "in": "query",
                                        "schema": {"type": "boolean"}}]},
                "put": {"requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["title"],
                    "properties": {"title": {"type": "string"}, "n": {"type": "integer"}}}}}}},
            },
            "/ping": {"get": {}},
        },
    }
    ops = {(o.method, o.path_pattern): o for o in parse_openapi(doc)}
    assert set(ops) == {("GET", "/items/{id}"), ("PUT", "/items/{id}"), ("GET", "/ping")}
    get = {(p["in"], p["name"]): p for p in ops[("GET", "/items/{id}")].params}
    assert get[("path", "id")]["required"] is True and get[("path", "id")]["type"] == "integer"
    assert get[("query", "verbose")]["type"] == "boolean"
    put = {(p["in"], p["name"]): p for p in ops[("PUT", "/items/{id}")].params}
    assert put[("body", "title")]["required"] is True and put[("body", "n")]["required"] is False
    assert isinstance(ops[("GET", "/ping")], SchemaOp)


def test_parse_openapi_v2_basepath_and_body():
    doc = {"swagger": "2.0", "basePath": "/v1",
           "paths": {"/x": {"post": {"parameters": [
               {"name": "q", "in": "query", "type": "string"},
               {"name": "b", "in": "body", "schema": {"$ref": "#/definitions/B"}},
               {"name": "f", "in": "formData", "type": "string"}]}}},
           "definitions": {"B": {"properties": {"name": {"type": "string"}}}}}
    (op,) = parse_openapi(doc)
    assert (op.method, op.path_pattern) == ("POST", "/v1/x")
    assert {(p["in"], p["name"]) for p in op.params} == {
        ("query", "q"), ("body", "name"), ("body", "f")}


def test_parse_introspection():
    result = {"data": {"__schema": {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
        "subscriptionType": None,
        "types": [
            {"name": "Query", "kind": "OBJECT", "fields": [{"name": "items"}, {"name": "me"}]},
            {"name": "Mutation", "kind": "OBJECT", "fields": [{"name": "deleteItem"}]},
            {"name": "Item", "kind": "OBJECT", "fields": [{"name": "id"}]},
            {"name": "__Schema", "kind": "OBJECT", "fields": [{"name": "types"}]}]}}}
    assert parse_introspection(result) == [
        ("query", "items"), ("query", "me"), ("mutation", "deleteItem")]
    assert parse_introspection({"errors": [{"message": "no"}]}) == []
    assert parse_introspection(None) == []


@pytest.fixture
async def api():
    async with async_playwright() as p:
        ctx = await p.request.new_context()
        yield ctx
        await ctx.dispose()


async def test_probe_schemas_graphql_introspection_live(api, spa_url, make_cfg, run_dir,
                                                        tmp_path):
    from appmap.crawl import build_runtime
    cfg = make_cfg(spa_url)
    rd = run_dir(tmp_path, cfg)
    rt = build_runtime(cfg, rd)
    rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                            known_patterns=[])
    client = DirectClient(rt.gate, rec, rt.events)
    counts = await probe_schemas(client, api, cfg, rt.store, "anonymous")
    rows = [dict(r) for r in rt.store.conn.execute(
        "SELECT e.graphql_op, e.graphql_type, s.source FROM endpoints e JOIN sources s "
        "ON s.item_kind='endpoint' AND s.item_id=e.id WHERE e.kind='graphql_op' "
        "AND s.source='graphql' AND e.graphql_op IN ('ListItems','items','deleteItem')")]
    assert counts["graphql_ops"] >= 2
    assert {r["graphql_op"] for r in rows} >= {"items", "deleteItem"}
    types = {r["graphql_op"]: r["graphql_type"] for r in rows}
    assert types["items"] == "query" and types["deleteItem"] == "mutation"
    # idempotent
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    rt.store.close()
