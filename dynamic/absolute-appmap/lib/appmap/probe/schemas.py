"""OpenAPI/Swagger and GraphQL introspection discovery."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..direct import DirectClient
from ..normalize import normalize_path
from ..store import Store
from ._common import base_origins, expired, with_source

OPENAPI_PATHS = ("/openapi.json", "/swagger.json", "/v3/api-docs", "/api-docs",
                 "/swagger/v1/swagger.json", "/api/openapi.json")
GRAPHQL_PATHS = ("/graphql", "/api/graphql")
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_IN = {"path": "path", "query": "query", "header": "header", "cookie": "cookie",
       "body": "body", "formData": "body"}

INTROSPECTION_QUERY = """query IntrospectionQuery {
  __schema {
    queryType { name } mutationType { name } subscriptionType { name }
    types { name kind fields(includeDeprecated: true) { name } }
  }
}"""


@dataclass
class SchemaOp:
    method: str
    path_pattern: str
    params: list[dict] = field(default_factory=list)  # {name, in, required, type}


log = logging.getLogger("appmap.probe.schemas")


def _flat_type(t):
    """OpenAPI 3.1 allows ``type: ["string", "null"]``; keep one non-null type name."""
    if isinstance(t, list):
        t = next((x for x in t if isinstance(x, str) and x != "null"), None)
    return t if isinstance(t, str) else None


def _dicts(value) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _ref(doc: dict, node):
    """Resolve local ``$ref`` chains (bounded)."""
    for _ in range(10):
        if not (isinstance(node, dict) and isinstance(node.get("$ref"), str)):
            break
        cur = doc
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur.get(part) if isinstance(cur, dict) else None
        node = cur
    return node if isinstance(node, dict) else {}


def _param(doc: dict, raw) -> dict | None:
    p = _ref(doc, raw)
    if not isinstance(p.get("name"), str):
        return None
    loc = _IN.get(p.get("in"))
    if not loc or not p.get("name"):
        return None
    schema = _ref(doc, p.get("schema")) if "schema" in p else p
    return {"name": p["name"], "in": loc, "required": p.get("required") is True or loc == "path",
            "type": _flat_type(schema.get("type"))}


def _body_params(doc: dict, schema) -> list[dict]:
    schema = _ref(doc, schema)
    req = schema.get("required")
    required = {r for r in req if isinstance(r, str)} if isinstance(req, list) else set()
    props = schema.get("properties")
    return [{"name": n, "in": "body", "required": n in required,
             "type": _flat_type(_ref(doc, s).get("type"))}
            for n, s in (props.items() if isinstance(props, dict) else []) if isinstance(n, str)]


def parse_openapi(doc: dict) -> list[SchemaOp]:
    """Operations of an OpenAPI 3 or Swagger 2 document."""
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        return []
    prefix = ""
    if "swagger" in doc:
        prefix = (doc.get("basePath") or "").rstrip("/")
    elif _dicts(doc.get("servers")):
        prefix = urlsplit(str(doc["servers"][0].get("url", ""))).path.rstrip("/")
    ops: list[SchemaOp] = []
    for path, item in doc["paths"].items():
        if not isinstance(item, dict):
            continue
        item = _ref(doc, item)
        shared = [p for p in (_param(doc, r) for r in _dicts(item.get("parameters"))) if p]
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = {(p["in"], p["name"]): p for p in shared}
            for raw in _dicts(op.get("parameters")):
                r = _ref(doc, raw)
                if r.get("in") == "body":
                    for p in _body_params(doc, r.get("schema")):
                        params[(p["in"], p["name"])] = p
                elif (p := _param(doc, r)):
                    params[(p["in"], p["name"])] = p
            content = _ref(doc, op.get("requestBody")).get("content")
            for media in (content.values() if isinstance(content, dict) else []):
                for p in _body_params(doc, media.get("schema") if isinstance(media, dict) else None):
                    params[(p["in"], p["name"])] = p
            ops.append(SchemaOp(method.upper(), prefix + str(path), list(params.values())))
    return ops


def parse_introspection(result) -> list[tuple[str, str]]:
    """``(operation_type, field_name)`` for every root field of an introspection result."""
    if not isinstance(result, dict):
        return []
    data = result.get("data") if "data" in result else result
    schema = data.get("__schema") if isinstance(data, dict) else None
    if not isinstance(schema, dict):
        return []
    types = {t.get("name"): t for t in schema.get("types") or [] if isinstance(t, dict)}
    out: list[tuple[str, str]] = []
    for kind in ("query", "mutation", "subscription"):
        root = schema.get(f"{kind}Type")
        t = types.get((root or {}).get("name")) if isinstance(root, dict) else None
        for f in (t or {}).get("fields") or []:
            if isinstance(f, dict) and f.get("name"):
                out.append((kind, f["name"]))
    return out


async def probe_schemas(client: DirectClient, api, cfg, store: Store, role: str, *,
                        deadline: float | None = None) -> dict[str, int]:
    counts = {"requests": 0, "openapi_ops": 0, "graphql_ops": 0, "timed_out": 0}
    scope = client.gate.scope
    origins = base_origins(cfg)
    known = [r["pattern"] for r in store.conn.execute(
        "SELECT pattern FROM routes WHERE kind='spa_route'")]

    def alive() -> bool:
        if expired(deadline):
            counts["timed_out"] = 1
            return False
        return True

    # -- OpenAPI ------------------------------------------------------------
    candidates = [o + p for o in origins for p in OPENAPI_PATHS]
    for (url,) in store.conn.execute(
            "SELECT DISTINCT url FROM exchanges WHERE (url LIKE '%openapi%' OR url LIKE '%swagger%') "
            "AND lower(substr(url, 1, instr(url || '?', '?') - 1)) LIKE '%.json'"):
        if url not in candidates and scope.allows(url):
            candidates.append(url)
    for url in candidates:
        if not alive():
            return counts
        got = await with_source(client, "openapi", attribute="ok_nonhtml").request(
            api, "GET", url, role=role, origin="passive")
        counts["requests"] += 1
        if got is None or got[0] != 200:
            continue
        try:
            ops = parse_openapi(json.loads(await got[1].text()))
        except ValueError:
            continue
        except Exception as exc:  # malformed real-world document: skip it, keep probing
            log.warning("openapi %s: unparseable document (%s: %s)", url, type(exc).__name__, exc)
            if client.events is not None:
                client.events.emit("error", role=role, url=url,
                                   error=f"openapi parse failed: {type(exc).__name__}: {exc}")
            continue
        parts = urlsplit(url)
        host = parts.netloc.lower()
        for op in ops:
            if not scope.allows(f"{parts.scheme}://{parts.netloc}{op.path_pattern}"):
                continue
            eid = store.upsert_endpoint(op.method, host, normalize_path(op.path_pattern, known),
                                        "rest")
            store.upsert_source("endpoint", eid, "openapi")
            for p in op.params:
                store.upsert_param(eid, p["in"], p["name"],
                                   types=[p["type"]] if p["type"] else None,
                                   required_evidence="schema" if p["required"] else None)
            counts["openapi_ops"] += 1

    # -- GraphQL ------------------------------------------------------------
    targets: list[str] = [o + p for o in origins for p in GRAPHQL_PATHS]
    scheme = {urlsplit(o).netloc: urlsplit(o).scheme for o in origins}
    for row in store.conn.execute(
            "SELECT DISTINCT host, path_pattern FROM endpoints WHERE kind='graphql_op'"):
        url = f"{scheme.get(row['host'], urlsplit(origins[0]).scheme)}://{row['host']}{row['path_pattern']}"
        if url not in targets:
            targets.append(url)
    for url in targets:
        if not alive():
            return counts
        got = await with_source(client, "graphql", attribute=False).request(
            api, "POST", url, role=role, origin="passive", json={"query": INTROSPECTION_QUERY})
        counts["requests"] += 1
        if got is None or got[0] != 200:
            continue
        try:
            fields = parse_introspection(json.loads(await got[1].text()))
        except Exception:  # not JSON / unexpected shape: no operations to record
            continue
        parts = urlsplit(url)
        for kind, name in fields:
            eid = store.upsert_endpoint("POST", parts.netloc.lower(), parts.path or "/",
                                        "graphql_op", graphql_op=name, graphql_type=kind)
            store.upsert_source("endpoint", eid, "graphql")
            counts["graphql_ops"] += 1
    return counts
