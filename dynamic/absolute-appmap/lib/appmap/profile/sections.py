"""Descriptive profile sections I: layout, data model and environment.

Read-only over a run's SQLite store and ``run.json``; nothing here contacts the target.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from appmap.probe.schemas import load_spec
from appmap.profile.evidence import MAX_EVIDENCE_TEXT, _is_schema, _read_body
from appmap.redact import Redactor
from appmap.store import Store

_redactor = Redactor([], [])
_NON_ENDPOINT_KINDS = ("static", "page")
_VERSION_SEG = re.compile(r"^v\d+$", re.I)
_PLACEHOLDER = re.compile(r"^\{[^}]*\}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TYPE_NAMES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
               "null": "null", "dict": "object", "list": "array"}
_CONSTRAINT_KEYS = ("format", "minLength", "maxLength", "minimum", "maximum", "pattern",
                    "minItems", "maxItems", "exclusiveMinimum", "exclusiveMaximum")
MAX_EXAMPLE = 80


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _segments(pattern: str) -> list[str]:
    return [s for s in (pattern or "").split("?")[0].strip("/").split("/") if s]


# --------------------------------------------------------------------------- layout
def _area_prefixes(pattern: str) -> list[str]:
    segs = _segments(pattern)
    if not segs:
        return ["/"]
    first = "/" + segs[0]
    if segs[0] != "api":
        return [first]
    out = [first]
    if len(segs) > 1 and not _PLACEHOLDER.match(segs[1]) and not _VERSION_SEG.match(segs[1]):
        out.append(first + "/" + segs[1])
    return out


def _resource(pattern: str) -> str | None:
    """Resource segment of an endpoint path: first segment after ``api`` and version."""
    segs = _segments(pattern)
    if segs and segs[0] == "api":
        segs = segs[1:]
    while segs and _VERSION_SEG.match(segs[0]):
        segs = segs[1:]
    if not segs or _PLACEHOLDER.match(segs[0]):
        return None
    return segs[0]


def layout(store: Store, run_id: str) -> dict:
    conn = store.conn
    areas: dict[str, dict] = {}

    def area(prefix: str) -> dict:
        return areas.setdefault(prefix, {"prefix": prefix, "routes": [], "endpoints": [],
                                         "roles": set()})

    route_roles: dict[tuple[str, int], set[str]] = {}
    for r in conn.execute("SELECT item_kind, item_id, role FROM role_access "
                          "WHERE outcome='allowed'"):
        route_roles.setdefault((r["item_kind"], r["item_id"]), set()).add(r["role"])
    visited: dict[str, set[str]] = {}
    for s in conn.execute("SELECT DISTINCT route_pattern, role FROM states "
                          "WHERE route_pattern IS NOT NULL AND role IS NOT NULL"):
        visited.setdefault(s["route_pattern"], set()).add(s["role"])

    for r in conn.execute("SELECT id, pattern FROM routes ORDER BY pattern"):
        for prefix in _area_prefixes(r["pattern"]):
            a = area(prefix)
            if r["pattern"] not in a["routes"]:
                a["routes"].append(r["pattern"])
            a["roles"] |= route_roles.get(("route", r["id"]), set()) | visited.get(
                r["pattern"], set())
    groups: dict[str, list[str]] = {}
    for e in conn.execute("SELECT id, method, path_pattern, kind FROM endpoints "
                          "ORDER BY path_pattern, method"):
        if e["kind"] in _NON_ENDPOINT_KINDS:
            continue
        label = f"{e['method']} {e['path_pattern']}"
        for prefix in _area_prefixes(e["path_pattern"]):
            a = area(prefix)
            a["endpoints"].append(label)
            a["roles"] |= route_roles.get(("endpoint", e["id"]), set())
        res = _resource(e["path_pattern"])
        if res:
            groups.setdefault(res, []).append(label)

    entry: dict[str, list[str]] = {}
    for s in conn.execute("SELECT role, url FROM states WHERE role IS NOT NULL ORDER BY rowid"):
        urls = entry.setdefault(s["role"], [])
        url = _redactor.url(s["url"] or "")
        if url and url not in urls and len(urls) < 3:
            urls.append(url)

    return {
        "areas": [{"prefix": a["prefix"], "routes": a["routes"], "endpoints": a["endpoints"],
                   "roles": sorted(a["roles"])} for _, a in sorted(areas.items())],
        "entry_points": entry,
        "api_groups": [{"resource": k, "endpoints": v} for k, v in sorted(groups.items())],
    }


# ------------------------------------------------------------------------ data model
def _example(name: str, value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    red = _redactor.json({name: value}).get(name)
    return str(red)[:MAX_EXAMPLE]


def _schema_type(doc: dict, node, depth: int = 0) -> str:
    if not isinstance(node, dict):
        return "unknown"
    ref = node.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    t = node.get("type")
    if isinstance(t, list):
        t = next((x for x in t if isinstance(x, str) and x != "null"), None)
    if t == "array" and depth < 3:
        return f"array<{_schema_type(doc, node.get('items'), depth + 1)}>"
    return t if isinstance(t, str) else ("object" if "properties" in node else "unknown")


def _openapi_entities(doc: dict, ref: dict) -> list[dict]:
    comps = (doc.get("components") or {}).get("schemas") if isinstance(
        doc.get("components"), dict) else None
    schemas = comps if isinstance(comps, dict) else (
        doc.get("definitions") if isinstance(doc.get("definitions"), dict) else {})
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    out = []
    for name, sch in schemas.items():
        if not isinstance(sch, dict) or not isinstance(sch.get("properties"), dict):
            continue
        required = set(sch.get("required") or []) if isinstance(sch.get("required"), list) else set()
        fields = []
        for fname, prop in sch["properties"].items():
            prop = prop if isinstance(prop, dict) else {}
            fields.append({
                "name": fname, "type": _schema_type(doc, prop),
                "required": fname in required,
                "constraints": {k: prop[k] for k in _CONSTRAINT_KEYS if k in prop},
                "enum": [str(v) for v in prop["enum"]] if isinstance(prop.get("enum"), list) else [],
                "example_redacted": _example(fname, prop.get("example")),
            })
        needle = f'"#/{"components/schemas" if comps is not None else "definitions"}/{name}"'
        endpoints = []
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if isinstance(op, dict) and needle in json.dumps(op) \
                        and f"{method.upper()} {path}" not in endpoints:
                    endpoints.append(f"{method.upper()} {path}")
        id_fmt = _id_from_schema(sch["properties"].get("id"))
        out.append({"name": name, "source": "openapi", "fields": fields, "id_format": id_fmt,
                    "endpoints": endpoints, "evidence": [ref]})
    return out


def _id_from_schema(prop) -> str:
    if not isinstance(prop, dict):
        return "unknown"
    if prop.get("format") == "uuid":
        return "uuid"
    if prop.get("type") == "integer":
        return "int"
    return "unknown"


def _gql_type(t, depth: int = 0) -> tuple[str, bool]:
    required = False
    while isinstance(t, dict) and t.get("kind") in ("NON_NULL", "LIST") and depth < 6:
        if t["kind"] == "NON_NULL":
            required = True
            t = t.get("ofType")
        else:
            inner, _ = _gql_type(t.get("ofType"), depth + 1)
            return f"list<{inner}>", required
        depth += 1
    name = t.get("name") if isinstance(t, dict) else None
    return (name or "unknown"), required


def _graphql_entities(result, ref: dict) -> list[dict]:
    data = result.get("data") if isinstance(result, dict) and "data" in result else result
    schema = data.get("__schema") if isinstance(data, dict) else None
    if not isinstance(schema, dict):
        return []
    roots = {(schema.get(k) or {}).get("name") for k in
             ("queryType", "mutationType", "subscriptionType") if isinstance(schema.get(k), dict)}
    out = []
    for t in schema.get("types") or []:
        if not (isinstance(t, dict) and t.get("kind") == "OBJECT") or not t.get("name") \
                or str(t["name"]).startswith("__") or t["name"] in roots:
            continue
        fields = []
        for f in t.get("fields") or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            ftype, req = _gql_type(f.get("type"))
            fields.append({"name": f["name"], "type": ftype, "required": req,
                           "constraints": {}, "enum": [], "example_redacted": None})
        idf = next((f for f in fields if f["name"] == "id"), None)
        id_fmt = "unknown" if not idf else ("int" if idf["type"] == "Int" else "unknown")
        out.append({"name": t["name"], "source": "graphql", "fields": fields,
                    "id_format": id_fmt, "endpoints": [], "evidence": [ref]})
    return out


def _schema_entities(store: Store, run_id: str, run_dir: Path | None) -> list[dict]:
    root = run_dir or store.db_path.parent
    out = []
    for row in store.conn.execute(
            "SELECT id, origin, url, status, resp_body, resp_body_path, content_type "
            "FROM exchanges ORDER BY id"):
        body = _read_body(root, row["resp_body"], row["resp_body_path"])
        if not body or not _is_schema(row, body):
            continue
        ref = {"run_id": run_id, "kind": "exchange", "id": row["id"]}
        doc = load_spec(body[:MAX_EVIDENCE_TEXT])
        if not isinstance(doc, dict):
            continue
        if "openapi" in doc or "swagger" in doc:
            out += _openapi_entities(doc, ref)
        elif "__schema" in doc or "__schema" in (doc.get("data") or {}):
            out += _graphql_entities(doc, ref)
    return out


def _shape_fields(shape) -> dict[str, str]:
    if isinstance(shape, list):
        shape = shape[0] if shape else None
    if not isinstance(shape, dict):
        return {}
    out = {}
    for k, v in shape.items():
        out[k] = _TYPE_NAMES.get(v, v) if isinstance(v, str) else (
            "array" if isinstance(v, list) else "object")
    return out


def _id_format(param_name: str, types: list, examples: list) -> str:
    for ex in examples:
        s = str(ex)
        if _UUID.match(s):
            return "uuid"
    if any(t in ("int", "integer") for t in types):
        return "int"
    for ex in examples:
        if str(ex).isdigit():
            return "int"
    name = param_name.lower()
    if name in ("uuid", "guid"):
        return "uuid"
    if name == "id":
        return "int"
    if name in ("slug", "name", "handle"):
        return "slug"
    return "unknown"


def _inferred_entities(store: Store, run_id: str) -> list[dict]:
    conn = store.conn
    res: dict[str, dict] = {}

    def entity(name: str) -> dict:
        return res.setdefault(name, {"fields": {}, "endpoints": [], "ids": [], "refs": [],
                                     "shaped": False, "item": False})

    eps = conn.execute("SELECT id, method, path_pattern, kind FROM endpoints "
                       "WHERE kind != 'static' ORDER BY id").fetchall()
    ep_res: dict[int, str] = {}
    for e in eps:
        name = _resource(e["path_pattern"])
        if not name:
            continue
        ep_res[e["id"]] = name
        ent = entity(name)
        label = f"{e['method']} {e['path_pattern']}"
        if label not in ent["endpoints"]:
            ent["endpoints"].append(label)
        ent["refs"].append({"run_id": run_id, "kind": "endpoint", "id": e["id"]})
        segs = _segments(e["path_pattern"])
        if segs and segs[0] == "api":
            segs = segs[1:]
        for i, seg in enumerate(segs):
            if _PLACEHOLDER.match(seg) and i > 0:
                ent["item"] = True
                pname = seg.strip("{}")
                p = conn.execute("SELECT types, examples FROM params WHERE endpoint_id=? AND "
                                 "location='path' AND name=?", (e["id"], pname)).fetchone()
                ent["ids"].append(_id_format(pname, _loads(p["types"], []) if p else [],
                                             _loads(p["examples"], []) if p else []))
                break

    for r in conn.execute("SELECT endpoint_id, statuses, body_shape, sample_exchange_ids "
                          "FROM responses ORDER BY id"):
        name = ep_res.get(r["endpoint_id"])
        if not name:
            continue
        statuses = _loads(r["statuses"], [])
        if statuses and not any(isinstance(s, int) and 200 <= s < 300 for s in statuses):
            continue
        fields = _shape_fields(_loads_any(r["body_shape"]))
        if fields:
            ent = entity(name)
            ent["shaped"] = True
            for k, t in fields.items():
                ent["fields"].setdefault(k, {"type": t, "required": False, "example": None})
            for xid in _loads(r["sample_exchange_ids"], [])[:1]:
                ent["refs"].append({"run_id": run_id, "kind": "exchange", "id": xid})

    for p in conn.execute("SELECT endpoint_id, name, types, examples, required_evidence "
                          "FROM params WHERE location='body' ORDER BY id"):
        name = ep_res.get(p["endpoint_id"])
        if not name:
            continue
        types = _loads(p["types"], [])
        examples = _loads(p["examples"], [])
        f = entity(name)["fields"].setdefault(
            p["name"], {"type": _TYPE_NAMES.get(types[0], types[0]) if types else "unknown",
                        "required": False, "example": None})
        f["required"] = f["required"] or bool(p["required_evidence"])
        f["example"] = f["example"] or (_example(p["name"], examples[0]) if examples else None)

    for fm in conn.execute("SELECT id, action_endpoint_id FROM forms "
                           "WHERE action_endpoint_id IS NOT NULL ORDER BY id"):
        name = ep_res.get(fm["action_endpoint_id"])
        if not name:
            continue
        ent = entity(name)
        ent["refs"].append({"run_id": run_id, "kind": "form", "id": fm["id"]})
        for fld in conn.execute("SELECT name, type, widget, constraints, options FROM fields "
                                "WHERE form_id=? ORDER BY id", (fm["id"],)):
            if not fld["name"] or (fld["type"] or "") in ("password", "hidden", "submit") \
                    or "csrf" in fld["name"].lower():
                continue
            cons = _loads(fld["constraints"], {})
            f = ent["fields"].setdefault(
                fld["name"], {"type": fld["type"] or fld["widget"] or "unknown",
                              "required": False, "example": None})
            f["required"] = f["required"] or bool(cons.get("required"))
            f.setdefault("constraints", {}).update(
                {k: v for k, v in cons.items() if k != "required"})
            opts = _loads(fld["options"], [])
            if opts:
                f["enum"] = [str(o.get("value", o) if isinstance(o, dict) else o) for o in opts]

    out = []
    for name, ent in sorted(res.items()):
        if not ent["fields"] or not (ent["shaped"] or ent["item"]):
            continue
        ids = [i for i in ent["ids"] if i != "unknown"]
        out.append({
            "name": name, "source": "inferred",
            "fields": [{"name": k, "type": v["type"], "required": v["required"],
                        "constraints": v.get("constraints", {}), "enum": v.get("enum", []),
                        "example_redacted": v["example"]} for k, v in ent["fields"].items()],
            "id_format": ids[0] if ids else "unknown",
            "endpoints": ent["endpoints"], "evidence": ent["refs"][:10],
        })
    return out


def _loads_any(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _key(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    return n[:-1] if n.endswith("s") else n


def data_model(store: Store, run_id: str, run_dir: Path | None = None) -> list[dict]:
    declared = _schema_entities(store, run_id, run_dir)
    seen = {_key(e["name"]) for e in declared}
    inferred = [e for e in _inferred_entities(store, run_id) if _key(e["name"]) not in seen]
    return declared + inferred


# ------------------------------------------------------------------------ environment
def environment(run_dir: Path) -> dict:
    meta = json.loads((Path(run_dir) / "run.json").read_text())
    cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    base_urls = [_redactor.url(u) for u in cfg.get("base_urls") or []]
    hosts: list[str] = []
    for u in base_urls:
        h = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u).split("/")[0]
        if h and h not in hosts:
            hosts.append(h)
    return {
        "run_id": meta.get("run_id"),
        "base_urls": base_urls,
        "hosts": hosts,
        "profile": meta.get("profile"),
        "started_at": meta.get("started_at"),
        "tool_version": meta.get("tool_version"),
        "contains_production_data": bool(meta.get("contains_production_data")),
    }
