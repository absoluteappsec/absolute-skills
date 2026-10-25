"""Evidence extraction: turns a run's stored exchanges, routes and roles into typed
``Evidence`` records for the profile detectors. Read-only; cookie values are never emitted."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import parse_qsl, urlsplit

from appmap.probe.schemas import DOCS_UI_PATHS, OPENAPI_PATHS
from appmap.redact import Redactor
from appmap.rundir import open_run_dir
from appmap.store import Store

MAX_EVIDENCE_TEXT = 2 * 1024 * 1024  # per-body scan cap (2 MiB)

Kind = Literal["header", "cookie", "html", "script", "source_map", "schema", "spa",
               "error_page", "auth"]

_ERROR_MARKERS = (
    "You're seeing this error because you have DEBUG = True", "Traceback (most recent call last)",
    "Whitelabel Error Page", "Server Error (500)", "Werkzeug Debugger", "Exception Details",
    "Application Error", "Stack trace:", "Fatal error:", "Internal Server Error",
)
_SCHEMA_PATHS = tuple(p.split("?")[0].rstrip("/") or "/" for p in OPENAPI_PATHS + DOCS_UI_PATHS)
_JSON_SPEC = re.compile(r'^\s*\{\s*"(?:openapi|swagger)"\s*:')
_YAML_SPEC = re.compile(r"^(?:openapi|swagger)\s*:", re.M)
_SRCMAP_SOURCES = re.compile(r'"sources"\s*:\s*\[(.*?)\]', re.S)
_TOKEN_HEADERS = ("authorization", "x-csrf-token", "x-xsrf-token", "x-api-key", "x-auth-token")
_redactor = Redactor([], [])


@dataclass(frozen=True)
class Evidence:
    run_id: str
    kind: Kind
    ref: dict
    url: str
    text: str
    headers: dict[str, str]


def _read_body(root: Path, inline: str | None, path: str | None) -> str | None:
    """Inline body, else the bodies/ file; only the first ``MAX_EVIDENCE_TEXT`` is read."""
    if inline is not None:
        return inline[:MAX_EVIDENCE_TEXT]
    if not path:
        return None
    try:
        p = (root / path).resolve()
        if not p.is_relative_to(root.resolve()):
            return None
        with open(p, "rb") as fh:
            return fh.read(MAX_EVIDENCE_TEXT).decode("utf-8", errors="replace")
    except OSError:
        return None


def _loads(raw: str | None, default):
    try:
        v = json.loads(raw) if raw else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _cookie_names(value) -> list[str]:
    lines = value if isinstance(value, list) else str(value).split("\n")
    names = []
    for line in lines:
        name = str(line).partition(";")[0].partition("=")[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _path(url: str) -> str:
    return urlsplit(url or "").path


def _is_schema(row, body: str | None) -> bool:
    path = _path(row["url"]).rstrip("/") or "/"
    if row["origin"] == "schema" or (path in _SCHEMA_PATHS and (row["status"] or 0) < 400):
        return True
    if not body:
        return False
    head = body[:4096]
    if _JSON_SPEC.match(head) or _YAML_SPEC.search(head):
        return True
    return "__schema" in body and "json" in (row["content_type"] or "").lower()


def _source_map_text(body: str) -> str:
    try:
        obj = json.loads(body)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        sources = [str(s) for s in obj.get("sources") or []]
        contents = obj.get("sourcesContent") or []
        heads = [c[:200] for c in contents[:50] if isinstance(c, str)]
    else:  # truncated by the scan cap: salvage the sources list
        m = _SRCMAP_SOURCES.search(body)
        sources = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if m else []
        heads = []
    return "\n".join(sources + heads)[:MAX_EVIDENCE_TEXT]


def _body_kind(row, body: str) -> str | None:
    path = _path(row["url"]).lower()
    ct = (row["content_type"] or "").lower()
    if path.endswith(".map"):
        return "source_map"
    if "javascript" in ct or "ecmascript" in ct or path.endswith((".js", ".mjs")):
        return "script"
    if "html" in ct or body.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        if (row["status"] or 0) >= 400 or any(m in body for m in _ERROR_MARKERS):
            return "error_page"
        return "html"
    return None


def _auth_evidence(store: Store, run_id: str) -> Iterator[Evidence]:
    for role in store.conn.execute("SELECT name, login_type, auth_ok FROM roles ORDER BY name"):
        fields: list[str] = []
        token_headers: list[str] = []
        first = None
        for x in store.conn.execute(
                "SELECT id, url, req_headers, req_body, resp_headers FROM exchanges "
                "WHERE origin='login' AND role=? ORDER BY id", (role["name"],)):
            first = first or x
            body = x["req_body"] or ""
            try:
                parsed = json.loads(body)
                names = list(parsed) if isinstance(parsed, dict) else []
            except ValueError:
                names = [k for k, _ in parse_qsl(body[:65536], keep_blank_values=True)]
            fields += [n for n in names if n not in fields]
            for blob in (x["req_headers"], x["resp_headers"]):
                for h in _loads(blob, {}):
                    if h.lower() in _TOKEN_HEADERS and h.lower() not in token_headers:
                        token_headers.append(h.lower())
        text = (f"role={role['name']} login_type={role['login_type'] or ''} "
                f"auth_ok={role['auth_ok']} fields={','.join(fields)} "
                f"token_headers={','.join(token_headers)}")
        ref = {"run_id": run_id, "kind": "exchange", "id": first["id"]} if first else \
            {"run_id": run_id, "kind": "file", "id": "appmap.sqlite"}
        yield Evidence(run_id, "auth", ref, first["url"] if first else "", text, {})


def iter_evidence(run_dir: Path) -> Iterator[Evidence]:
    rd = open_run_dir(Path(run_dir))
    run_id = rd.run_id
    store = Store.open_readonly(rd.db_path)
    try:
        cur = store.conn.execute(
            "SELECT id, origin, url, status, resp_headers, resp_body, resp_body_path, "
            "content_type FROM exchanges ORDER BY id")
        for row in cur:
            ref = {"run_id": run_id, "kind": "exchange", "id": row["id"]}
            url = _redactor.url(row["url"] or "")
            raw_headers = _loads(row["resp_headers"], {})
            headers = {}
            cookies: list[str] = []
            for name, value in raw_headers.items():
                if str(name).lower() == "set-cookie":
                    cookies = _cookie_names(value)
                else:
                    headers[str(name)] = value if isinstance(value, str) else json.dumps(value)
            headers = _redactor.headers(headers)
            if raw_headers:
                text = "\n".join(f"{k}: {v}" for k, v in headers.items())
                yield Evidence(run_id, "header", ref, url, text[:MAX_EVIDENCE_TEXT], headers)
            for name in cookies:
                yield Evidence(run_id, "cookie", ref, url, name, {})
            body = _read_body(rd.root, row["resp_body"], row["resp_body_path"])
            if body is None:
                continue
            if _is_schema(row, body):
                yield Evidence(run_id, "schema", ref, url, body, headers)
                continue
            kind = _body_kind(row, body)
            if kind == "source_map":
                body = _source_map_text(body)
            if kind:
                yield Evidence(run_id, kind, ref, url, body, headers)
        spa = store.conn.execute(
            "SELECT id, pattern FROM routes WHERE kind='spa_route' ORDER BY id").fetchall()
        if spa:
            yield Evidence(run_id, "spa", {"run_id": run_id, "kind": "route", "id": spa[0]["id"]},
                           "", "\n".join(r["pattern"] for r in spa), {})
        yield from _auth_evidence(store, run_id)
    finally:
        store.close()
