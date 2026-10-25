"""Descriptive profile sections I: layout, data model and environment.

Read-only over a run's SQLite store and ``run.json``; nothing here contacts the target.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from appmap.build import created_records, marker_variants, run_marker
from appmap.gaps import compute_gaps
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
    if not segs or _PLACEHOLDER.match(segs[0]) or "." in segs[0]:
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
_SENSITIVE_NAME = re.compile(r"token|csrf|xsrf|secret|passw|api[_-]?key|credential|session|"
                             r"authoriz|jwt|bearer|private[_-]?key", re.I)
_IDENTITY_NAME = re.compile(r"user[_-]?name|e[_-]?mail|login|owner|^user$|^author$|phone", re.I)
_WRAPPER_KEY = re.compile(r"cursor|page|next|prev|total|count|limit|offset|size|has_more|more",
                          re.I)
_NON_DATA_TYPES = {"div", "span", "button", "submit", "reset", "image", "a", "link"}
_NON_DATA_WIDGETS = {"button", "link", "tab", "menuitem", "menu"}
_FORM_TYPES = {"text": "string", "email": "string", "url": "string", "tel": "string",
               "search": "string", "textarea": "string", "select": "string",
               "select-one": "string", "radio": "string", "number": "number",
               "range": "number", "checkbox": "boolean", "date": "date",
               "datetime-local": "datetime", "time": "time", "month": "string",
               "week": "string", "file": "file", "color": "string"}


def _example(name: str, value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if _SENSITIVE_NAME.search(name) or _IDENTITY_NAME.search(name):
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
        id_fmt = "int" if idf and idf["type"] == "Int" else "unknown"  # ID alone says nothing
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
    shape = _unwrap(shape)
    if not isinstance(shape, dict):
        return {}
    out = {}
    for k, v in shape.items():
        if _SENSITIVE_NAME.search(k):
            continue
        out[k] = _TYPE_NAMES.get(v, v) if isinstance(v, str) else (
            "array" if isinstance(v, list) else "object")
    return out


def _id_format(param_name: str, types: list, examples: list) -> str:
    exs = [str(e) for e in examples]
    if any(_UUID.match(e) for e in exs):
        return "uuid"
    if any(t in ("int", "integer") for t in types) or (exs and all(e.isdigit() for e in exs)):
        return "int"
    name = param_name.lower()
    if name in ("uuid", "guid"):
        return "uuid"
    if name in ("slug", "handle"):
        return "slug"
    return "unknown"


def _unwrap(shape):
    """Item shape of a response: lists and list wrappers (an array-of-objects field next to
    cursor/page/count-style siblings) unwrap to the element."""
    if isinstance(shape, list):
        return shape[0] if shape and isinstance(shape[0], dict) else None
    if not isinstance(shape, dict):
        return None
    lists = [k for k, v in shape.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
    if len(lists) == 1 and all(k == lists[0] or _WRAPPER_KEY.search(k) for k in shape):
        return shape[lists[0]][0]
    return shape


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
        if _SENSITIVE_NAME.search(p["name"]):
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
            ftype = (fld["type"] or "").lower()
            if not fld["name"] or ftype in {"password", "hidden"} | _NON_DATA_TYPES \
                    or (fld["widget"] or "").lower() in _NON_DATA_WIDGETS \
                    or _SENSITIVE_NAME.search(fld["name"]):
                continue
            if fld["name"].lower() in {k.lower() for k in ent["fields"]} - {fld["name"]}:
                continue  # a UI label for a field already seen under its wire name
            cons = _loads(fld["constraints"], {})
            f = ent["fields"].setdefault(
                fld["name"], {"type": _FORM_TYPES.get(ftype, "string" if ftype else "unknown"),
                              "required": False, "example": None})
            f["required"] = f["required"] or bool(cons.get("required"))
            f.setdefault("constraints", {}).update(
                {k: v for k, v in cons.items() if k != "required"})
            opts = _loads(fld["options"], [])
            if opts:
                f["enum"] = [str(o.get("value", o) if isinstance(o, dict) else o) for o in opts]

    out = []
    for name, ent in sorted(res.items()):
        if not ent["fields"] or not ent["item"]:  # singletons (login, stats, graphql) are not entities
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
    declared: dict[str, dict] = {}
    for e in _schema_entities(store, run_id, run_dir):
        cur = declared.setdefault(_key(e["name"]), e)
        if cur is e:
            continue
        have = {f["name"] for f in cur["fields"]}
        cur["fields"] += [f for f in e["fields"] if f["name"] not in have]
        cur["endpoints"] += [x for x in e["endpoints"] if x not in cur["endpoints"]]
        cur["evidence"] += [r for r in e["evidence"] if r not in cur["evidence"]]
        if cur["id_format"] == "unknown":
            cur["id_format"] = e["id_format"]
    seen = set(declared)
    declared = list(declared.values())
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
        "contains_production_data": bool(meta.get("contains_production_data"))
        or meta.get("profile") == "production",
    }


# ------------------------------------------------------- sections II: features etc.
_OBSERVED_CAP = 50
_OBSERVED_KEYS = {"username", "email", "user", "login"}
_EMAIL = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}$")
_NAME = re.compile(r"^[A-Za-z0-9][\w.@+-]{0,63}$")
_MAILTO = re.compile(r"mailto:([^\"'<>\s?#&]{3,200})", re.I)
_SIGNUP_PATH = re.compile(
    r"(?:^|/)(?:regist\w*|sign[-_]?up|join|users?|accounts?|members?|create-?account|"
    r"new-?user)(?:/|$|\.)", re.I)
_BODY_ID_KEY = re.compile(r"^(?:user[_-]?name|e[_-]?mail|login|user)$", re.I)
_TOKENISH = (re.compile(r"^[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}$"), re.compile(r"^[0-9a-fA-F]{16,}$"),
             re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$"))
MAX_CREATED = 100
_LIST_CAP = 5000
_NOISE_OUTCOMES = {"not_tested"}


def _exref(run_id: str, xid: int) -> dict:
    return {"run_id": run_id, "kind": "exchange", "id": xid}


def _flow_resource(pre: dict) -> str | None:
    steps = pre.get("steps") or []
    ordered = [s for s in steps if s.get("mutating")] + steps
    for s in ordered:
        res = _resource(s.get("path_pattern") or "")
        if res:
            return res
    return None


def features(store: Store, run_id: str) -> list[dict]:
    """Feature candidates: flows, forms, routes and endpoints clustered by resource."""
    conn = store.conn
    feats: dict[str, dict] = {}

    def feat(res: str) -> dict:
        return feats.setdefault(res, {"flow_keys": set(), "routes": [], "endpoints": [],
                                      "forms": [], "roles": set()})

    access: dict[tuple[str, int], set[str]] = {}
    for r in conn.execute("SELECT item_kind, item_id, role FROM role_access "
                          "WHERE outcome='allowed'"):
        access.setdefault((r["item_kind"], r["item_id"]), set()).add(r["role"])
    for r in conn.execute("SELECT id, pattern FROM routes ORDER BY pattern"):
        res = _resource(r["pattern"])
        if res:
            f = feat(res)
            f["routes"].append(r["pattern"])
            f["roles"] |= access.get(("route", r["id"]), set())
    ep_res: dict[int, str] = {}
    for e in conn.execute("SELECT id, method, path_pattern, kind FROM endpoints "
                          "ORDER BY path_pattern, method"):
        res = _resource(e["path_pattern"])
        if not res or e["kind"] in _NON_ENDPOINT_KINDS or e["method"] in ("HEAD", "OPTIONS"):
            continue
        ep_res[e["id"]] = res
        f = feat(res)
        f["endpoints"].append(f"{e['method']} {e['path_pattern']}")
        f["roles"] |= access.get(("endpoint", e["id"]), set())
    for fm in conn.execute(
            "SELECT f.id, f.method, f.purpose, f.action_endpoint_id, e.path_pattern "
            "FROM forms f LEFT JOIN endpoints e ON e.id=f.action_endpoint_id ORDER BY f.id"):
        res = _resource(fm["path_pattern"] or "")
        if res:
            feat(res)["forms"].append({"id": fm["id"], "method": fm["method"],
                                       "purpose": fm["purpose"] or "",
                                       "action": fm["path_pattern"]})
    for fl in conn.execute("SELECT role, preconditions FROM flows ORDER BY id"):
        pre = _loads(fl["preconditions"], {})
        res = _flow_resource(pre)
        if res and pre.get("flow_key"):
            f = feat(res)
            f["flow_keys"].add(pre["flow_key"])
            if fl["role"]:
                f["roles"].add(fl["role"])
    out = []
    for res, f in sorted(feats.items()):
        keys = sorted(f["flow_keys"])
        out.append({
            "key": "ft-" + hashlib.sha1((res + "|" + "|".join(keys)).encode()).hexdigest()[:12],
            "resource": res, "flow_keys": keys, "routes": f["routes"],
            "endpoints": f["endpoints"], "forms": f["forms"], "roles": sorted(f["roles"]),
            "name_suggestion": re.sub(r"[-_]+", " ", res).strip().title(),
        })
    return out


def _created_ids(store: Store, records: list[dict]) -> set[str]:
    ids: set[str] = set()
    for r in records:
        m = re.search(r"(\d+)/?$", urlsplit(r.get("location") or "").path)
        if m:
            ids.add(m.group(1))
        row = store.conn.execute("SELECT resp_body FROM exchanges WHERE id=?",
                                 (r["id"],)).fetchone()
        doc = _loads_any(row["resp_body"]) if row and row["resp_body"] else None
        if isinstance(doc, dict) and isinstance(doc.get("id"), (int, str)):
            ids.add(str(doc["id"]))
    return ids


def _item_dicts(doc) -> list[dict]:
    if isinstance(doc, dict):
        lists = [v for v in doc.values() if isinstance(v, list)]
        doc = lists[0] if len(lists) == 1 else [doc]
    return [d for d in doc[:_LIST_CAP] if isinstance(d, dict)] if isinstance(doc, list) else []


def _seed_observations(store: Store, created: set[str]) -> list[dict]:
    root = store.db_path.parent
    ids: dict[str, set[int]] = {}
    for x in store.conn.execute(
            "SELECT x.resp_body, x.resp_body_path, e.path_pattern FROM exchanges x "
            "JOIN endpoints e ON e.id=x.endpoint_id WHERE x.method='GET' "
            "AND x.status BETWEEN 200 AND 299 AND e.kind NOT IN ('static','page') "
            "AND (x.content_type LIKE '%json%') ORDER BY x.id"):
        res = _resource(x["path_pattern"])
        if not res:
            continue
        body = _read_body(root, x["resp_body"], x["resp_body_path"])
        doc = _loads_any(body) if body else None
        for d in _item_dicts(doc):
            v = d.get("id")
            if isinstance(v, int) and not isinstance(v, bool) and str(v) not in created:
                ids.setdefault(res, set()).add(v)
    return [{"resource": res, "id_min": min(v), "id_max": max(v), "count": len(v)}
            for res, v in sorted(ids.items())]


def data_setup(store: Store, run_id: str) -> dict:
    pre = []
    for fl in store.conn.execute("SELECT preconditions FROM flows ORDER BY id"):
        p = _loads(fl["preconditions"], {})
        reqs = [{"endpoint": r.get("endpoint"), "json_path": r.get("json_path"),
                 "consumer_location": r.get("consumer_location"),
                 "evidence": _exref(run_id, r["producer_exchange_id"])
                 if r.get("producer_exchange_id") is not None else None}
                for r in p.get("requires") or []]
        if reqs and p.get("flow_key"):
            pre.append({"flow_key": p["flow_key"], "requires": reqs})
    records = created_records(store)
    meta_path = store.db_path.parent / "run.json"
    try:
        cfg = json.loads(meta_path.read_text()).get("config") or {}
    except (OSError, ValueError):
        cfg = {}
    form_data = cfg.get("form_data") if isinstance(cfg.get("form_data"), dict) else {}
    fixtures = bool(form_data.get("fixtures")) or (store.db_path.parent / "fixtures.yaml").exists()
    return {
        "preconditions": pre,
        "created_records_more": max(len(records) - MAX_CREATED, 0),
        "created_records": [{
            "exchange_id": r["id"], "role": r["role"], "method": r["method"],
            "endpoint": r.get("path_pattern") or urlsplit(_redactor.url(r["url"] or "")).path,
            "status": r["status"], "marker": r.get("marker") or "",
            "ref": _exref(run_id, r["id"])} for r in records[:MAX_CREATED]],
        "fixtures_used": fixtures,
        "seed_observations": _seed_observations(store, _created_ids(store, records)),
    }


def mask_identity(s: str) -> str:
    """``chris@example.com`` -> ``c***@example.com``; ``chris`` -> ``c***``."""
    if not s:
        return ""
    local, at, domain = s.partition("@")
    return (local[:1] + "***" + at + domain) if local else s


def _tokenish(v: str) -> bool:
    """Values that look like tokens rather than account names."""
    part = v.split("@", 1)[0]
    return (any(rx.match(part) for rx in _TOKENISH)
            or (len(part) >= 12 and not re.search(r"[aeiouAEIOU]", part))
            or _redactor.text(v) != v)


def _identities(doc, depth: int = 0):
    if depth > 6:
        return
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, str) and str(k).lower() in _OBSERVED_KEYS \
                    and not _SENSITIVE_NAME.search(str(k)):
                if (_EMAIL.match(v) or (str(k).lower() != "email" and _NAME.match(v))) \
                        and not _tokenish(v):
                    yield v
            elif isinstance(v, (dict, list)):
                yield from _identities(v, depth + 1)
    elif isinstance(doc, list):
        for v in doc[:200]:
            yield from _identities(v, depth + 1)


def _observed_identities(store: Store, run_id: str) -> list[tuple[str, int]]:
    root = store.db_path.parent
    seen: dict[str, int] = {}
    for x in store.conn.execute(
            "SELECT id, content_type, resp_body, resp_body_path FROM exchanges "
            "WHERE status BETWEEN 200 AND 299 ORDER BY id"):
        if len(seen) >= _OBSERVED_CAP:
            break
        ct = (x["content_type"] or "").lower()
        is_json, is_html = "json" in ct, "html" in ct
        if not (is_json or is_html):
            continue
        body = _read_body(root, x["resp_body"], x["resp_body_path"])
        if not body:
            continue
        found: list[str] = []
        if is_json:
            doc = _loads_any(body)
            found += list(_identities(doc)) if doc is not None else []
        else:
            found += [m for m in (unquote(m) for m in _MAILTO.findall(body))
                      if _EMAIL.match(m)]
        for v in found:
            if len(seen) < _OBSERVED_CAP:
                seen.setdefault(v, x["id"])
    return list(seen.items())


def _flat_pairs(doc, out: list, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, str):
                out.append((str(k), v))
            else:
                _flat_pairs(v, out, depth + 1)
    elif isinstance(doc, list):
        for v in doc[:50]:
            _flat_pairs(v, out, depth + 1)


def _body_identities(store: Store, exchange_id: int) -> list[tuple[str, str]]:
    """Identity-keyed (key, value) pairs of a request body: JSON, urlencoded or multipart."""
    x = store.conn.execute("SELECT req_body, req_body_path FROM exchanges WHERE id=?",
                           (exchange_id,)).fetchone()
    body = _read_body(store.db_path.parent, x["req_body"], x["req_body_path"]) if x else None
    if not body:
        return []
    pairs: list[tuple[str, str]] = []
    doc = _loads_any(body) if body.lstrip()[:1] in "{[" else None
    if doc is not None:
        _flat_pairs(doc, pairs)
    else:
        pairs += parse_qsl(body, keep_blank_values=False) if "Content-Disposition" not in body \
            else re.findall(r'name="([^"]+)"\r?\n(?:[^\r\n]+\r?\n)*\r?\n([^\r\n]*)', body)
    out = []
    for k, v in pairs:
        v = v.strip()
        if _BODY_ID_KEY.match(k) and 0 < len(v) <= 120 and not _tokenish(v):
            out.append((k.lower(), v))
    return out


def accounts(store: Store, run_id: str, *, mask: bool) -> dict:
    conn = store.conn
    counts: dict[tuple[str, str], int] = {}
    for r in conn.execute("SELECT role, item_kind, count(*) n FROM role_access "
                          "WHERE outcome='allowed' GROUP BY role, item_kind"):
        counts[(r["role"], r["item_kind"])] = r["n"]
    roles = [{"name": r["name"], "login_type": r["login_type"],
              "auth_ok": None if r["auth_ok"] is None else bool(r["auth_ok"]),
              "reachable_routes": counts.get((r["name"], "route"), 0),
              "reachable_endpoints": counts.get((r["name"], "endpoint"), 0)}
             for r in conn.execute("SELECT name, login_type, auth_ok FROM roles ORDER BY name")]
    marker = run_marker(store)
    variants = marker_variants(store, marker) if marker else set()

    def is_ours(v: str) -> bool:
        return any(m and m in v for m in variants)

    def shown(v: str | None) -> str | None:
        if v is None:
            return None
        return v if (is_ours(v) or not mask) else mask_identity(v)

    created = []
    for r in created_records(store):
        path = urlsplit(r["url"] or "").path
        ids = _body_identities(store, r["id"])
        if not ids:
            continue
        if not (any(is_ours(v) for _, v in ids) or _SIGNUP_PATH.search(path)):
            continue
        email = next((v for _, v in ids if _EMAIL.match(v)), None)
        user = next((v for k, v in ids if v != email and not _EMAIL.match(v)), None)
        created.append({
            "exchange_id": r["id"],
            "endpoint": r.get("path_pattern") or urlsplit(_redactor.url(r["url"] or "")).path,
            "username": shown(user), "email": shown(email), "ref": _exref(run_id, r["id"])})
    observed = [{"value": (v if (is_ours(v) or not mask) else mask_identity(v)),
                 "where": _exref(run_id, xid)}
                for v, xid in _observed_identities(store, run_id)]
    return {"roles": roles, "created_by_run": created, "observed": observed}


def _outcome(outcomes: set[str]) -> str:
    real = outcomes - _NOISE_OUTCOMES
    if not real:
        return "not_tested"
    return next(iter(real)) if len(real) == 1 else "mixed(" + ",".join(sorted(real)) + ")"


def coverage(store: Store, run_id: str) -> dict:
    conn = store.conn
    outcomes: dict[tuple[str, int], dict[str, set[str]]] = {}
    for r in conn.execute("SELECT item_kind, item_id, role, outcome FROM role_access"):
        outcomes.setdefault((r["item_kind"], r["item_id"]), {}).setdefault(
            r["role"], set()).add(r["outcome"])
    route_id = {r["pattern"]: r["id"] for r in conn.execute("SELECT id, pattern FROM routes")}
    ep_id = {f"{e['method']} {e['path_pattern']}": e["id"] for e in conn.execute(
        "SELECT id, method, path_pattern FROM endpoints")}
    role_names = [r["name"] for r in conn.execute("SELECT name FROM roles ORDER BY name")]
    grid: dict[str, dict[str, str]] = {}
    for f in features(store, run_id):
        items = [("route", route_id[p]) for p in f["routes"] if p in route_id] + \
                [("endpoint", ep_id[e]) for e in f["endpoints"] if e in ep_id]
        grid[f["key"]] = {
            role: _outcome({o for it in items for o in outcomes.get(it, {}).get(role, set())})
            for role in role_names}
    dec = {r["decision"]: r["n"] for r in conn.execute(
        "SELECT decision, count(*) n FROM approvals GROUP BY decision")}
    return {
        "features_x_roles": grid,
        "unexercised": compute_gaps(store).summary(),
        "approvals": {"pending": dec.get("pending", 0), "denied": dec.get("deny", 0),
                      "skipped": dec.get("skip", 0)},
    }
