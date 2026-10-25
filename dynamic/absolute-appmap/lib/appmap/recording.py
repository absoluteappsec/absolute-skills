"""Exchange recording: redact, persist, and index one request/response pair."""
from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .events import EventLog
from .normalize import normalize_path
from .redact import Redactor
from .rundir import RunDir
from .store import Store

INLINE_LIMIT = 256 * 1024
STATIC_EXTS = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
               ".woff2", ".ttf", ".map", ".webp")
_SCAN_LIMIT = 256 * 1024
_MAX_EXAMPLES = 5
_MAX_SAMPLES = 5
_GQL_OP = re.compile(r"^\s*(query|mutation|subscription)\s+(\w+)")
_GQL_TYPE = re.compile(r"^\s*(query|mutation|subscription)\b")

# "passive" has no default source: callers must say which probe found the exchange.
_SOURCE_BY_ORIGIN = {"ui": "ui", "approved": "ui", "login": "ui", "replay": "replay",
                     "cross_role": "replay", "schema": "openapi", "bundle": "bundle"}
# Higher wins; an endpoint's kind is never downgraded by a later exchange.
_KIND_RANK = {"page": 0, "rest": 1, "static": 2, "websocket": 3, "sse": 4, "graphql_op": 5}

_STACK = re.compile("|".join([
    r"Traceback \(most recent call last\)",                 # Python
    r"\bat [\w.$<>]+\([\w$]+\.java:\d+\)",                   # Java
    r"Exception in thread \"",                               # Java
    r"\bat [\w.<>`]+\(.*\) in .+:line \d+",                  # .NET
    r"\bSystem\.\w+Exception\b",                             # .NET
    r"\bat (?:async )?[\w.<>$]+ \(.+:\d+:\d+\)",             # Node
    r"\.rb:\d+:in [`']",                                     # Ruby
]))
_DEBUG = re.compile("|".join([
    r"Werkzeug Debugger", r"werkzeug\.debugger", r"The debugger caught an exception",
    r"You're seeing this error because you have <code>DEBUG = True",
    r"Action Controller: Exception caught", r"Rails\.root:",
    r"Whoops! There was an error", r"Illuminate\\\\",
]))
_CAPTCHA = re.compile(r"g-recaptcha|google\.com/recaptcha|hcaptcha\.com|h-captcha|"
                      r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)


def _hget(headers: dict, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return str(v)
    return None


def _base_ct(ct: str | None) -> str:
    return (ct or "").split(";", 1)[0].strip().lower()


def _is_text_ct(ct: str | None) -> bool:
    b = _base_ct(ct)
    return (not b or b.startswith("text/") or "json" in b or "xml" in b
            or "javascript" in b or "graphql" in b or b == "application/x-www-form-urlencoded")


def _decode(body: bytes | None, ct: str | None) -> str | None:
    """Text form of a body, or None when absent or binary."""
    if not body or not _is_text_ct(ct):
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _snippet(body: str, m: re.Match) -> str:
    start = max(0, m.start() - 20)
    return body[start:m.end() + 80].strip()[:160]


def detect_observations(status: int, body: str | None, content_type: str | None,
                        headers: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if status >= 500:
        out.append(("server_error", f"HTTP {status}"))
    if status == 429 or _hget(headers, "retry-after") is not None:
        out.append(("rate_limited", f"HTTP {status}"))
    if body:
        body = body[:_SCAN_LIMIT]
        for kind, pat in (("stack_trace", _STACK), ("debug_page", _DEBUG), ("captcha", _CAPTCHA)):
            m = pat.search(body)
            if m:
                out.append((kind, _snippet(body, m)))
    return out


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    return "list" if isinstance(v, list) else "dict"


def body_shape(v: Any) -> Any:
    """JSON key skeleton with leaf type names; arrays collapse to their first element."""
    if isinstance(v, dict):
        return {k: body_shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [body_shape(v[0])] if v else []
    return _type_name(v)


def _json_or_none(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _graphql(req_text: str | None) -> tuple[str, str | None] | None:
    obj = _json_or_none(req_text)
    if not isinstance(obj, dict) or not isinstance(obj.get("query"), str):
        return None
    q = obj["query"]
    m = _GQL_OP.match(q)
    if m:
        return m.group(2), m.group(1)
    t = _GQL_TYPE.match(q)
    op_name = obj.get("operationName")
    return (op_name if isinstance(op_name, str) else ""), (t.group(1) if t else "query")


def _kind(path: str, resp_ct: str | None, gql: bool) -> str:
    base = _base_ct(resp_ct)
    if gql:
        return "graphql_op"
    if base == "text/event-stream":
        return "sse"
    if path.lower().endswith(STATIC_EXTS):
        return "static"
    if base in ("text/html", "application/xhtml+xml"):
        return "page"
    return "rest"


def _merge(old: list | None, new: list, cap: int | None = None) -> list:
    out = list(old or [])
    for x in new:
        if x not in out:
            out.append(x)
    return out[:cap] if cap else out


def _row_json(store: Store, sql: str, *args) -> tuple[Any, ...] | None:
    return store.conn.execute(sql, args).fetchone()


def _ext(ct: str | None, text: bool) -> str:
    base = _base_ct(ct)
    if "json" in base:
        return "json"
    if base == "text/plain":
        return "txt"
    ext = mimetypes.guess_extension(base) if base else None
    if ext == ".jpe":
        ext = ".jpg"
    return (ext or (".txt" if text else ".bin")).lstrip(".")


def _upsert_param(store: Store, redactor: Redactor, endpoint_id: int, location: str,
                  name: str, value: Any) -> None:
    row = _row_json(store, "SELECT types, examples FROM params WHERE endpoint_id=? AND "
                    "location=? AND name=?", endpoint_id, location, name)
    types = json.loads(row["types"]) if row and row["types"] else []
    examples = json.loads(row["examples"]) if row and row["examples"] else []
    safe = redactor.json({name: value}).get(name)
    store.upsert_param(endpoint_id, location, name,
                       types=_merge(types, [_type_name(value)]),
                       examples=_merge(examples, [safe], _MAX_EXAMPLES))


def _index_params(store: Store, redactor: Redactor, endpoint_id: int, raw_segs: list[str],
                  pattern: str, query: str, req_ct: str | None, req_text: str | None) -> None:
    for seg, pseg in zip(raw_segs, [s for s in pattern.split("/") if s]):
        if pseg.startswith("{") and pseg.endswith("}"):
            _upsert_param(store, redactor, endpoint_id, "path", pseg[1:-1], seg)
    for k, v in parse_qsl(query, keep_blank_values=True):
        _upsert_param(store, redactor, endpoint_id, "query", k, v)
    if req_text is None:
        return
    if is_multipart(req_ct):
        for k, v in multipart_fields(req_text.encode("latin-1"), req_ct):
            _upsert_param(store, redactor, endpoint_id, "body", k, v)
        return
    if "x-www-form-urlencoded" in (req_ct or "").lower():
        for k, v in parse_qsl(req_text, keep_blank_values=True):
            _upsert_param(store, redactor, endpoint_id, "body", k, v)
    else:
        obj = _json_or_none(req_text)
        if isinstance(obj, dict):
            for k, v in obj.items():
                _upsert_param(store, redactor, endpoint_id, "body", k, v)


_MP_BOUNDARY = re.compile(r"""\bboundary\s*=\s*(?:"([^"]+)"|([^;\s]+))""", re.I)
_MP_NAME = re.compile(rb"""\bname\s*=\s*(?:"([^"]*)"|([^;\s]+))""", re.I)
_MP_FILENAME = re.compile(rb"\bfilename\*?\s*=", re.I)


def is_multipart(content_type: str | None) -> bool:
    return _base_ct(content_type) == "multipart/form-data"


def _mp_boundary(content_type: str | None) -> bytes | None:
    m = _MP_BOUNDARY.search(content_type or "")
    return (m.group(1) or m.group(2)).encode("latin-1", "replace") if m else None


def _mp_split(body: bytes, boundary: bytes) -> list[tuple[bytes, bytes, bytes, bytes]] | None:
    """``[(head, sep, content, tail)]`` per part plus the framing kept around them, or None
    when ``body`` is not framed by ``boundary``. ``head`` holds the part's lead newline and
    headers, ``sep`` the blank line, ``tail`` the newline that belongs to the next delimiter."""
    chunks = body.split(b"--" + boundary)
    if len(chunks) < 3:
        return None
    parts = []
    for chunk in chunks[1:-1]:
        for sep in (b"\r\n\r\n", b"\n\n"):
            i = chunk.find(sep)
            if i >= 0:
                break
        else:
            return None
        head, content = chunk[:i], chunk[i + len(sep):]
        tail = b"\r\n" if content.endswith(b"\r\n") else b"\n" if content.endswith(b"\n") else b""
        parts.append((head, sep, content[:len(content) - len(tail)], tail))
    return parts


def _mp_name(head: bytes) -> tuple[str | None, bool]:
    """Field name of a part and whether it is a file upload."""
    for line in head.splitlines():
        if line.lower().startswith(b"content-disposition"):
            m = _MP_NAME.search(line)
            name = (m.group(1) if m.group(1) is not None else m.group(2)) if m else None
            return (name.decode("utf-8", "replace") if name is not None else None,
                    bool(_MP_FILENAME.search(line)))
    return None, False


def multipart_fields(body: bytes, content_type: str | None) -> list[tuple[str, str]]:
    """``(name, value)`` of every non-file part (values decoded as UTF-8)."""
    boundary = _mp_boundary(content_type)
    parts = _mp_split(body, boundary) if boundary else None
    out = []
    for head, _sep, content, _tail in parts or []:
        name, is_file = _mp_name(head)
        if name is not None and not is_file:
            out.append((name, content.decode("utf-8", "replace")))
    return out


def _lat(redact, raw: bytes) -> bytes:
    return redact(raw.decode("latin-1")).encode("latin-1", errors="replace")


def redact_multipart(redactor: Redactor, body: bytes, content_type: str | None) -> bytes:
    """Redact a ``multipart/form-data`` body part by part, keeping its framing: text parts
    by field name (the urlencoded/JSON key rules: password, csrf, token, secret, api_key),
    then as text; file parts keep their bytes apart from literal-secret scrubbing. A body
    that does not parse as multipart is scrubbed as opaque bytes."""
    boundary = _mp_boundary(content_type)
    parts = _mp_split(body, boundary) if boundary else None
    if parts is None:
        return redact_bytes(redactor, body)
    delim = b"--" + boundary
    chunks = body.split(delim)
    out = [_lat(redactor.literals, chunks[0])]
    for head, sep, content, tail in parts:
        name, is_file = _mp_name(head)
        kind = redactor.key_kind(name) if name is not None and not is_file else None
        if kind:
            content = f"<redacted:{kind}>".encode()
        elif is_file:
            content = _lat(redactor.literals, content)
        else:
            content = _lat(redactor.text, content)
        out.append(_lat(redactor.text, head) + sep + content + tail)
    out.append(_lat(redactor.literals, chunks[-1]))
    return delim.join(out)


def redact_bytes(redactor: Redactor, raw: bytes) -> bytes:
    """Redact binary / non-UTF-8 content via a lossless latin-1 round trip."""
    return redactor.text(raw.decode("latin-1")).encode("latin-1", errors="replace")


def redact_text(redactor: Redactor, text: str, content_type: str | None = None) -> str:
    """JSON by key, form-urlencoded by parameter name (``authenticity_token=…``),
    multipart/form-data part by part (``redact_multipart``), anything else (HTML included:
    hidden CSRF inputs, meta tokens) as text."""
    if is_multipart(content_type):
        try:
            return redact_multipart(redactor, text.encode("latin-1"), content_type).decode("latin-1")
        except UnicodeEncodeError:  # real Unicode text: work on its UTF-8 bytes
            return redact_multipart(redactor, text.encode("utf-8", "surrogatepass"),
                                    content_type).decode("utf-8", "replace")
    if "x-www-form-urlencoded" in (content_type or "").lower():
        return redactor.url("x?" + text)[2:]
    obj = _json_or_none(text)
    if isinstance(obj, (dict, list)):
        return json.dumps(redactor.json(obj), ensure_ascii=False)
    return redactor.text(text)


def record_exchange(store: Store, events: EventLog, redactor: Redactor, rd: RunDir, *,
                    role: str, origin: str, method: str, url: str, req_headers: dict,
                    req_body: bytes | None, status: int, resp_headers: dict,
                    resp_body: bytes | None, duration_ms: float, caused_by: str | None,
                    known_patterns: list[str], source: str | None = None,
                    attribute: bool | str = True) -> int:
    """``attribute``: False records the exchange without endpoint/response rows; "ok"
    attributes only 2xx responses (guessed paths that 404 leave no map entries);
    "ok_nonhtml" also skips HTML answers (a catch-all SPA serves index.html for every
    guessed robots/sitemap/well-known/OpenAPI/bundle URL)."""
    if source is None:
        if origin not in _SOURCE_BY_ORIGIN:
            raise ValueError(f"origin {origin!r} has no default source; pass source=")
        source = _SOURCE_BY_ORIGIN[origin]
    parts = urlsplit(url)
    safe_url = redactor.url(url)
    safe_req_h = redactor.headers(req_headers or {})
    safe_resp_h = redactor.headers(resp_headers or {})
    req_ct = _hget(req_headers, "content-type")
    resp_ct = _hget(resp_headers, "content-type")
    req_text = _decode(req_body, req_ct)
    if req_body and is_multipart(req_ct):
        req_text = req_body.decode("latin-1")  # lossless view; parts are parsed as bytes
    resp_text = _decode(resp_body, resp_ct)

    req_ev = events.emit("request", role=role, caused_by=caused_by, method=method,
                         url=safe_url, headers=safe_req_h, origin=origin,
                         size=len(req_body or b""))
    resp_ev = events.emit("response", role=role, caused_by=req_ev.id, method=method,
                          url=safe_url, status=status, headers=safe_resp_h,
                          duration_ms=duration_ms, size=len(resp_body or b""))

    # Endpoint
    attributed = attribute is True or (attribute in ("ok", "ok_nonhtml")
                                       and 200 <= status < 300)
    if attribute == "ok_nonhtml" and _base_ct(resp_ct) in ("text/html", "application/xhtml+xml"):
        attributed = False
    gql = _graphql(req_text)
    path = parts.path or "/"
    endpoint_id = None
    if attributed:
        pattern = normalize_path(path, known_patterns)
        kind = _kind(path, resp_ct, gql is not None)
        host = parts.netloc.lower()
        gql_op, gql_type = gql if gql else ("", None)
        old = _row_json(store, "SELECT content_types, kind FROM endpoints WHERE method=? AND host=? "
                        "AND path_pattern=? AND graphql_op=?", method, host, pattern, gql_op)
        cts = json.loads(old["content_types"]) if old and old["content_types"] else []
        if _base_ct(resp_ct):
            cts = _merge(cts, [_base_ct(resp_ct)])
        if old and _KIND_RANK.get(old["kind"], 0) > _KIND_RANK[kind]:
            kind = old["kind"]
        endpoint_id = store.upsert_endpoint(method, host, pattern, kind, graphql_op=gql_op,
                                            graphql_type=gql_type, content_types=cts or None)
        if origin == "schema" and gql is not None and source == "openapi":
            source = "graphql"
        store.upsert_source("endpoint", endpoint_id, source)
        _index_params(store, redactor, endpoint_id, [s for s in path.split("/") if s], pattern,
                      parts.query, req_ct, req_text)

    # Bodies: text up to the inline limit stays in the row, the rest goes to bodies/
    inline: dict[str, str | None] = {"req": None, "resp": None}
    files: dict[str, tuple[bytes, str]] = {}  # side -> (data, ext)
    for side, raw, text, ct in (("req", req_body, req_text, req_ct),
                                ("resp", resp_body, resp_text, resp_ct)):
        if not raw:
            continue
        if side == "req" and is_multipart(ct):
            files[side] = (redact_multipart(redactor, raw, ct), "bin")
            continue
        if text is None:
            files[side] = (redact_bytes(redactor, raw), _ext(ct, False))
            continue
        safe = redact_text(redactor, text, ct)
        if len(safe.encode("utf-8")) <= INLINE_LIMIT:
            inline[side] = safe
        else:
            files[side] = (safe.encode("utf-8"), _ext(ct, True))

    exchange_id = store.add_exchange(
        endpoint_id, role, origin, event_id=resp_ev.id, method=method, url=safe_url,
        req_headers=safe_req_h, req_body=inline["req"], status=status,
        resp_headers=safe_resp_h, resp_body=inline["resp"],
        content_type=_base_ct(resp_ct) or None, duration_ms=duration_ms,
        ts=f"{resp_ev.ts:.6f}")
    paths: dict[str, str] = {}
    for side, (data, ext) in files.items():
        name = f"{exchange_id}.{ext}" if side == "resp" else f"{exchange_id}.req.{ext}"
        Path(rd.bodies_dir).mkdir(parents=True, exist_ok=True)
        (Path(rd.bodies_dir) / name).write_bytes(data)
        paths[side] = f"bodies/{name}"
    store.set_exchange_body_paths(exchange_id, req_body_path=paths.get("req"),
                                  resp_body_path=paths.get("resp"))

    # Response summary, merged with what earlier exchanges recorded
    if attributed:
        prev = _row_json(store, "SELECT * FROM responses WHERE endpoint_id=? AND role=?",
                         endpoint_id, role)
        j = lambda col: json.loads(prev[col]) if prev and prev[col] else []
        size = len(resp_body or b"")
        shape = body_shape(obj) if isinstance(obj := _json_or_none(resp_text), (dict, list)) else None
        seen_2xx = any(200 <= st < 300 for st in j("statuses"))
        if prev and prev["body_shape"] and (seen_2xx or not 200 <= status < 300):
            shape = None  # keep the first 2xx shape; errors never replace anything useful
        store.upsert_response(
            endpoint_id, role,
            statuses=sorted(set(j("statuses")) | {status}),
            content_types=_merge(j("content_types"), [_base_ct(resp_ct)] if _base_ct(resp_ct) else []),
            body_shape=shape,
            min_size=size if not prev or prev["min_size"] is None else min(prev["min_size"], size),
            max_size=size if not prev or prev["max_size"] is None else max(prev["max_size"], size),
            sample_exchange_ids=_merge(j("sample_exchange_ids"), [exchange_id], _MAX_SAMPLES))

    for okind, detail in detect_observations(status, resp_text, resp_ct, resp_headers or {}):
        store.add_observation(okind, redactor.text(detail), safe_url, role, resp_ev.id)
    return exchange_id
