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


def test_parse_openapi_malformed_and_31_types():
    doc = {"openapi": "3.1.0", "servers": ["x"], "paths": {
        "/a": {"parameters": "oops", "get": {
            "parameters": [{"name": "q", "in": "query", "required": "yes",
                            "schema": {"type": ["string", "null"]}}, "junk", {"in": "query"}],
            "requestBody": {"content": {"application/json": {"schema": {
                "required": True, "properties": ["bad"]}}, "text/plain": "nope"}}}},
        "/b": "not a dict", "/c": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"required": True, "properties": {"n": {"type": ["integer", "null"]}}}}}}}}}}
    ops = {(o.method, o.path_pattern): o for o in parse_openapi(doc)}
    assert set(ops) == {("GET", "/a"), ("POST", "/c")}
    (q,) = ops[("GET", "/a")].params
    assert q["type"] == "string" and q["required"] is False
    assert ops[("POST", "/c")].params == [
        {"name": "n", "in": "body", "required": False, "type": "integer"}]


async def test_malformed_openapi_does_not_abort_probe(api, make_cfg, run_dir, tmp_path,
                                                      stub_server):
    from werkzeug.wrappers import Response

    from appmap.crawl import build_runtime
    srv = stub_server({
        "/openapi.json": lambda r: Response('{"paths": {"/x": {"get": {"parameters": 5, '
                                            '"responses": null}}, "/y": 3}}',
                                            content_type="application/json"),
        "/swagger.json": lambda r: Response('{"paths": [1]}', content_type="application/json"),
        "/api/openapi.json": lambda r: Response(
            '{"openapi":"3.0.0","paths":{"/ok":{"get":{}}}}', content_type="application/json")})
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt = build_runtime(cfg, rd)
    rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                            known_patterns=[])
    counts = await probe_schemas(client=DirectClient(rt.gate, rec, rt.events), api=api,
                                 cfg=cfg, store=rt.store, role="anonymous")
    eps = {r[0] for r in rt.store.conn.execute("SELECT path_pattern FROM endpoints")}
    assert "/ok" in eps and counts["openapi_ops"] >= 2
    rt.store.close()


async def test_introspection_and_guesses_leave_no_fake_endpoints(api, spa_url, ssr_url,
                                                                 make_cfg, run_dir, tmp_path):
    from appmap.crawl import build_runtime
    for name, url in (("spa", spa_url), ("ssr", ssr_url)):
        cfg = make_cfg(url)
        rd = run_dir(tmp_path / name, cfg) if (tmp_path / name).mkdir() is None else None
        rt = build_runtime(cfg, rd)
        rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                                known_patterns=[])
        await probe_schemas(DirectClient(rt.gate, rec, rt.events), api, cfg, rt.store, "anonymous")
        ops = {r[0] for r in rt.store.conn.execute(
            "SELECT graphql_op FROM endpoints WHERE kind='graphql_op'")}
        assert "IntrospectionQuery" not in ops
        if name == "ssr":
            assert ops == set()
            paths = {r[0] for r in rt.store.conn.execute("SELECT path_pattern FROM endpoints")}
            assert "/graphql" not in paths and "/openapi.json" not in paths
            assert "/api/openapi.json" in paths
        # the attempts themselves are still recorded as exchanges
        assert rt.store.conn.execute(
            "SELECT count(*) FROM exchanges WHERE origin='passive'").fetchone()[0] >= 6
        rt.store.close()


# --- field test 1: drf-spectacular YAML, Swagger UI / ReDoc pages ----------------

_YAML_SPEC = """openapi: 3.0.3
info:
  title: Task Manager API
  version: 1.0.0
paths:
  /api/tasks/:
    get:
      operationId: tasks_list
      parameters:
      - in: query
        name: page
        schema:
          type: integer
      responses:
        '200':
          description: ''
  /api/tasks/{id}/:
    delete:
      parameters:
      - in: path
        name: id
        required: true
        schema:
          type: integer
      responses:
        '204':
          description: No response body
"""


def _rt(cfg, rd):
    from appmap.crawl import build_runtime
    rt = build_runtime(cfg, rd)
    rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                            known_patterns=[])
    return rt, DirectClient(rt.gate, rec, rt.events)


def _eps(store):
    return {(r[0], r[1]) for r in store.conn.execute(
        "SELECT e.method, e.path_pattern FROM endpoints e JOIN sources s ON "
        "s.item_kind='endpoint' AND s.item_id=e.id AND s.source='openapi'")}


async def test_yaml_schema_at_drf_spectacular_path(api, make_cfg, run_dir, tmp_path,
                                                   stub_server):
    from werkzeug.wrappers import Response
    srv = stub_server({"/api/schema/": lambda r: Response(
        _YAML_SPEC, content_type="application/vnd.oai.openapi; charset=utf-8")})
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _rt(cfg, rd)
    counts = await probe_schemas(client, api, cfg, rt.store, "anonymous")
    assert counts["openapi_ops"] == 2
    assert _eps(rt.store) >= {("GET", "/api/tasks"), ("DELETE", "/api/tasks/{id}")}
    rt.store.close()


async def test_yaml_schema_at_root_schema_path_and_html_rejected(api, make_cfg, run_dir,
                                                                 tmp_path, stub_server):
    from werkzeug.wrappers import Response
    srv = stub_server({
        "/schema/": lambda r: Response(_YAML_SPEC, content_type="application/yaml"),
        # a catch-all HTML page that happens to be valid YAML text is never a schema
        "/openapi.yaml": lambda r: Response("paths:\n  /evil/:\n    get: {}\n",
                                            content_type="text/html"),
    })
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _rt(cfg, rd)
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    eps = _eps(rt.store)
    assert ("GET", "/api/tasks") in eps and not any(p == "/evil" for _, p in eps)
    rt.store.close()


_SWAGGER_UI = """<!DOCTYPE html><html><head><title>Swagger</title></head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
const ui = SwaggerUIBundle({
  url: "\\u002Finternal\\u002Fspec\\u002F",
  dom_id: "#swagger-ui",
  oauth2RedirectUrl: "https://cdn.example/oauth2-redirect.html",
})
</script></body></html>"""

_REDOC = """<!DOCTYPE html><html><body>
<redoc spec-url='/internal/redoc-spec.json'></redoc>
<script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body></html>"""


async def test_swagger_ui_and_redoc_pages_spec_followed(api, make_cfg, run_dir, tmp_path,
                                                        stub_server):
    from werkzeug.wrappers import Response
    srv = stub_server({
        "/swagger-ui/": lambda r: Response(_SWAGGER_UI, mimetype="text/html"),
        "/internal/spec/": lambda r: Response(_YAML_SPEC,
                                              content_type="application/vnd.oai.openapi"),
        "/redoc/": lambda r: Response(_REDOC, mimetype="text/html"),
        "/internal/redoc-spec.json": lambda r: Response(
            '{"openapi":"3.0.0","paths":{"/api/redoc-only/":{"post":{}}}}',
            content_type="application/json"),
    })
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _rt(cfg, rd)
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    eps = _eps(rt.store)
    assert ("GET", "/api/tasks") in eps and ("POST", "/api/redoc-only") in eps
    fetched = {p for _, p in srv.requests}
    assert "/internal/spec/" in fetched and "/internal/redoc-spec.json" in fetched
    # the Swagger UI page itself is not an API endpoint
    paths = {r[0] for r in rt.store.conn.execute("SELECT path_pattern FROM endpoints")}
    assert "/swagger-ui" not in paths
    rt.store.close()


async def test_crawled_swagger_ui_page_spec_followed(api, make_cfg, run_dir, tmp_path,
                                                     stub_server):
    from werkzeug.wrappers import Response
    page = _SWAGGER_UI.replace("\\u002Finternal\\u002Fspec\\u002F", "/v9/custom-spec")
    srv = stub_server({
        "/v9/custom-spec": lambda r: Response(_YAML_SPEC, content_type="application/yaml")})
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _rt(cfg, rd)
    # a page the crawler recorded at an unguessable location
    record_exchange(rt.store, rt.events, rt.redactor, rd, role="anonymous", origin="ui",
                    method="GET", url=srv.url + "/dev/tools/api-browser", req_headers={},
                    req_body=None, status=200, resp_headers={"Content-Type": "text/html"},
                    resp_body=page.encode(), duration_ms=1.0, caused_by=None,
                    known_patterns=[])
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    assert ("GET", "/api/tasks") in _eps(rt.store)
    rt.store.close()


def test_spec_urls_from_docs_page():
    from appmap.probe.schemas import spec_urls_from_html
    base = "http://t.test/api/docs/"
    assert spec_urls_from_html(_SWAGGER_UI, base) == ["http://t.test/internal/spec/"]
    assert spec_urls_from_html(_REDOC, base) == ["http://t.test/internal/redoc-spec.json"]
    multi = """<script>SwaggerUIBundle({urls: [{url: "v1.json", name: "v1"},
        {"url": "/v2.yaml", "name": "v2"}], dom_id: '#x'})</script>"""
    assert spec_urls_from_html(multi, base) == ["http://t.test/api/docs/v1.json",
                                                "http://t.test/v2.yaml"]
    assert spec_urls_from_html("<p>url: 'x.json'</p>", base) == []
