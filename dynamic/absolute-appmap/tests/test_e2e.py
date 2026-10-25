"""Task 22: end-to-end acceptance against both fixture apps.

Runs the documented CLI workflow (skills/map-running-application, README) exactly as an
agent would — one subprocess per command, ``[sys.executable, "-m", "appmap", ...]`` from the
dev venv (ruling R3) — and checks the resulting run directory:

init -> auth -> probe --phase passive -> crawl (while in_progress) -> probe --phase active
-> approvals write -> (human decisions in approvals.yaml) -> approvals apply
-> crawl --approved-only -> crawl -> status --new-pass -> status --gaps -> build
"""
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from flask import request

from conftest import FIXTURE_ENV, StubServer, _load_app

PLUGIN = Path(__file__).resolve().parents[1]
PASSWORDS = (FIXTURE_ENV["APPMAP_ADMIN_PASS"], FIXTURE_ENV["APPMAP_ALICE_PASS"])
BUDGET = "1"          # minutes per crawl/probe invocation; loop until not in_progress
MAX_RERUNS = 12
LIMITS = {"max_requests_per_second": 200, "quiet_window_ms": 150,
          "max_states_per_route_pattern": 3}


_NOISE = re.compile(r"Warning|Traceback|Error", re.I)


# -- instrumented fixture servers ----------------------------------------------------
class Instrumented:
    """A fixture app on its own server, logging every request and every secret it issues."""

    def __init__(self, name: str):
        self.app = _load_app(name)
        self.log: list[dict] = []       # method, path, body (for POST /graphql)
        self.secrets: set[str] = set()  # sid cookie values and bearer tokens handed out

        @self.app.before_request
        def _log():
            entry = {"method": request.method, "path": request.path, "body": ""}
            if request.path == "/graphql":
                entry["body"] = request.get_data(cache=True, as_text=True)
            self.log.append(entry)

        @self.app.after_request
        def _capture(resp):
            for c in resp.headers.getlist("Set-Cookie"):
                m = re.match(r"sid=([^;]+)", c)
                if m and m.group(1):
                    self.secrets.add(m.group(1))
            if request.path == "/api/login" and resp.is_json and resp.status_code == 200:
                tok = (resp.get_json() or {}).get("token")
                if tok:
                    self.secrets.add(tok)
                    self.secrets.add(tok.rsplit(".", 1)[-1])  # the signature alone
            return resp

        self.server = StubServer(self.app)
        self.url = self.server.url


    def stop(self):
        self.server.stop()


# -- running the CLI like an agent ---------------------------------------------------
class Agent:
    def __init__(self, cache: Path):
        # "default" shows every warning once (incl. DeprecationWarning) so none hides
        self.env = {**os.environ, **FIXTURE_ENV, "XDG_CACHE_HOME": str(cache),
                    "PYTHONWARNINGS": "default"}
        self.statuses: list[dict] = []

    def run(self, *args: str, expect_ok: bool = True) -> tuple[dict, list[str]]:
        proc = subprocess.run([sys.executable, "-m", "appmap", *args], env=self.env,
                              capture_output=True, text=True, timeout=600)
        lines = proc.stdout.strip().splitlines()
        assert lines and lines[-1].startswith("APPMAP_STATUS "), (
            f"appmap {' '.join(args)}: no status line\nstdout:{proc.stdout}\n"
            f"stderr:{proc.stderr[-3000:]}")
        st = json.loads(lines[-1].removeprefix("APPMAP_STATUS "))
        noisy = [ln for ln in proc.stderr.splitlines() if _NOISE.search(ln)]
        assert not noisy, f"appmap {' '.join(args)} wrote warnings/errors:\n{proc.stderr}"
        assert set(st) == {"command", "ok", "status", "counts", "pending_approvals",
                           "errors", "next"}
        self.statuses.append(st)
        if expect_ok:
            assert st["ok"] is True, f"appmap {' '.join(args)} -> {st}\n{proc.stderr[-3000:]}"
            assert proc.returncode == 0
        return st, lines[:-1]

    def until_done(self, *args: str) -> dict:
        """Rerun the identical command while it reports in_progress (methodology)."""
        for _ in range(MAX_RERUNS):
            st, _ = self.run(*args)
            if st["status"] != "in_progress":
                return st
        pytest.fail(f"appmap {' '.join(args)} still in_progress after {MAX_RERUNS} runs")


def q(run: Path, sql: str, *args) -> list[dict]:
    conn = sqlite3.connect(f"file:{run / 'appmap.sqlite'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _decide(run: Path, created_ids: set[str]) -> dict:
    """What the human answers, recorded the way review-approvals Step 4 says.

    The human's policy is "approve deletes of records this run created, deny logout". It is
    recorded as a rule on the run marker plus per-item decisions: these fixture apps address
    records by numeric id, so the marker never appears in a predicted URL or label and the
    items whose URL/action path holds an id this run's writes returned are approved one by
    one. Everything else stays pending.
    """
    marker = "appmap-" + "-".join(run.name.split("-")[-2:])  # appmap-<run_id>
    path = run / "approvals.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    decided = {}
    for item in data.get("items") or []:
        if item["decision"] != "pending":
            continue
        pred = item.get("predicted_request") or {}
        where = q(run, "SELECT action_path FROM approvals WHERE id=?", item["id"])
        text = f"{pred.get('url') or ''} {item.get('label') or ''} {where[0]['action_path']}"
        if item["category"] == "logout":
            item["decision"], item["note"] = "deny", "operator: deny - never log out"
        elif item["category"] == "delete" and any(
                re.search(rf"/{i}(\b|/)", text) for i in created_ids):
            item["decision"] = "approve"
            item["note"] = "operator: approve - test record created by this run"
        decided[item["id"]] = item["decision"]
    data["rules"] = [{"match": {"category": "delete", "url_contains": marker},
                      "decision": "approve"}]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return decided


def workflow(server: Instrumented, tmp: Path, roles: list[dict], created_sql: str) -> dict:
    """The documented sequence. Returns run dir, agent and the decisions taken."""
    cfg = tmp / "appmap.config.yaml"
    cfg.write_text(yaml.safe_dump({
        "target": {"base_urls": [server.url],
                   "scope": {"allow_hosts": ["127.0.0.1"], "allow_path_prefixes": ["/"]}},
        "authorization": {"confirmed_by": "e2e test", "notes": "local fixture"},
        "profile": "staging",
        "limits": LIMITS,
        "roles": roles,
    }, sort_keys=False))
    agent = Agent(tmp / "cache")
    st, out = agent.run("init", "--config", str(cfg), "--out", str(tmp / "runs"))
    assert st["status"] == "initialized"
    run = Path(out[-1])
    assert run.is_dir() and (run / "run.json").is_file()
    r = str(run)

    st, _ = agent.run("auth", "--run", r)
    assert st["status"] == "authenticated", st
    st = agent.until_done("probe", "--run", r, "--phase", "passive", "--budget-minutes", BUDGET)
    assert st["status"] == "done", st
    st = agent.until_done("crawl", "--run", r, "--budget-minutes", BUDGET)
    assert st["status"] == "done", st
    st = agent.until_done("probe", "--run", r, "--phase", "active", "--budget-minutes", BUDGET)
    assert st["status"] == "done", st

    st, _ = agent.run("approvals", "--run", r, "write")
    assert st["status"] == "written"
    created = {str(x["id"]) for x in q(run, created_sql)}
    decided = _decide(run, created)
    st, _ = agent.run("approvals", "--run", r, "apply")
    assert st["status"] == "applied" and st["errors"] == [], st
    applied = st["counts"]
    st = agent.until_done("crawl", "--run", r, "--approved-only", "--budget-minutes", BUDGET)
    assert st["status"] == "done", st
    executed = st["counts"]
    st = agent.until_done("crawl", "--run", r, "--budget-minutes", BUDGET)
    assert st["status"] == "done", st

    st, _ = agent.run("status", "--run", r, "--new-pass")
    assert st["status"] == "new_pass"
    assert set(st["counts"]["new_in_prev_pass"]) == {"states", "endpoints", "routes"}
    st, out = agent.run("status", "--run", r, "--gaps")
    assert st["status"] == "reported"
    assert len(out) == 1 and "gaps" in json.loads(out[0])

    st, _ = agent.run("build", "--run", r)
    assert st["status"] == "complete"
    return {"run": run, "agent": agent, "decided": decided, "applied": applied,
            "executed": executed, "build": st["counts"]}


# -- shared checks -------------------------------------------------------------------
def _b64_forms(secret: bytes) -> set[bytes]:
    """Base64 (standard and URL-safe, padded or not) of ``secret`` as it would appear
    inside a longer encoded blob at any of the three byte alignments."""
    out = set()
    for k in range(3):
        enc = base64.b64encode(b"\0" * k + secret).rstrip(b"=")
        tail = 1 if (k + len(secret)) % 3 else 0   # last char also encodes the next byte
        core = enc[(0, 2, 3)[k]:len(enc) - tail]  # skip chars that encode the prefix
        out |= {core, core.translate(bytes.maketrans(b"+/", b"-_"))}
    out |= {base64.b64encode(secret), base64.urlsafe_b64encode(secret)}
    return out


def _har_decoded(data: bytes) -> bytes:
    """Base64-encoded HAR bodies (``content.encoding == "base64"``), decoded."""
    out = []
    for e in json.loads(data)["log"]["entries"]:
        for part in (e.get("response", {}).get("content", {}), e.get("request", {})
                     .get("postData", {})):
            if part.get("encoding") == "base64" and part.get("text"):
                out.append(base64.b64decode(part["text"] + "=" * (-len(part["text"]) % 4)))
    return b"\n".join(out)


def assert_no_secrets(run: Path, secrets: set[str]) -> None:
    """Review Focus 3: no password, raw sid cookie or JWT anywhere in the run directory —
    neither verbatim nor base64-encoded, and not inside base64 HAR bodies."""
    raw = {s.encode() for s in set(PASSWORDS) | secrets if s}
    assert len(raw) > len(PASSWORDS), "no session secret was captured from the fixture"
    needles = {n: n for n in raw}
    for n in raw:
        for form in _b64_forms(n):
            needles[form] = n
    hits = []
    files = [p for p in run.rglob("*") if p.is_file()]
    assert any(p.suffix == ".har" for p in files)
    for p in files:
        data = p.read_bytes()
        if p.suffix == ".har":
            data += b"\n" + _har_decoded(data)
        for form, n in needles.items():
            if form in data:
                hits.append(f"{p.relative_to(run)}: {n[:12]!r}... as {form[:16]!r}")
    assert not hits, hits


def test_secret_scan_catches_encoded_secrets(tmp_path):
    """The scanner itself: verbatim, base64/urlsafe at any alignment, and base64 HAR bodies."""
    sid = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    har = {"log": {"entries": [{"request": {}, "response": {"content": {
        "encoding": "base64",
        "text": base64.b64encode(b'{"pw": "' + PASSWORDS[0].encode() + b'"}').decode()}}}]}}
    cases = {
        "traffic/x.har": json.dumps(har).encode(),
        "a.txt": b"zz" + base64.urlsafe_b64encode(b"xy" + sid.encode() + b"q").rstrip(b"="),
        "b.txt": PASSWORDS[1].encode(),
    }
    for name, data in cases.items():
        run = tmp_path / name.replace("/", "_")
        (run / "traffic").mkdir(parents=True)
        (run / "traffic" / "clean.har").write_text('{"log": {"entries": []}}')
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_bytes(data)
        with pytest.raises(AssertionError, match=re.escape(name)):
            assert_no_secrets(run, {sid})


def assert_hosts_in_scope(run: Path) -> None:
    hosts = {urlsplit(x["url"]).hostname for x in q(run, "SELECT url FROM exchanges")}
    assert hosts and hosts <= {"127.0.0.1"}, hosts
    # the HAR may list a blocked attempt (status -1), never a completed out-of-scope request
    for har in (run / "traffic").glob("*.har"):
        for e in json.loads(har.read_text())["log"]["entries"]:
            if urlsplit(e["request"]["url"]).hostname != "127.0.0.1":
                assert e["response"]["status"] == -1, (har.name, e["request"]["url"])


def assert_outputs(run: Path) -> None:
    for name in ("APP-MAP.md", "appmap.json", "SCHEMA.md", "names.yaml", "approvals.yaml",
                 "events.jsonl", "crawl.log", "run.json"):
        assert (run / name).is_file(), name
    doc = json.loads((run / "appmap.json").read_text())
    assert doc and isinstance(doc, dict)
    assert json.loads((run / "run.json").read_text())["status"] == "complete"


# -- SSR -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ssr(tmp_path_factory):
    server = Instrumented("ssr")
    try:
        roles = [
            {"name": "admin", "login": {"type": "form", "url": f"{server.url}/login",
                                        "username_env": "APPMAP_ADMIN_USER",
                                        "password_env": "APPMAP_ADMIN_PASS"}},
            {"name": "alice", "login": {"type": "form", "url": f"{server.url}/login",
                                        "username_env": "APPMAP_ALICE_USER",
                                        "password_env": "APPMAP_ALICE_PASS"}},
            {"name": "anonymous", "login": {"type": "none"}},
        ]
        # project ids returned by this run's successful creations (redirect Location)
        created_sql = ("SELECT DISTINCT CAST(substr(json_extract(resp_headers, '$.location'),"
                       " length('/projects/') + 1) AS INTEGER) AS id FROM exchanges "
                       "WHERE method='POST' AND url LIKE '%/projects/new' AND status=302")
        res = workflow(server, tmp_path_factory.mktemp("ssr"), roles, created_sql)
        res["server"] = server
        yield res
    finally:
        server.stop()


def test_ssr_routes_access_and_outputs(ssr):
    run = ssr["run"]
    routes = {r["pattern"] for r in q(run, "SELECT pattern FROM routes WHERE kind='page'")}
    want = {"/", "/projects", "/projects/{id}", "/projects/new", "/admin",
            "/account/password", "/about", "/secret-report"}
    assert want <= routes, want - routes
    acc = {r["role"]: r["outcome"] for r in q(
        run, "SELECT ra.role, ra.outcome FROM role_access ra JOIN routes r ON r.id=ra.item_id "
             "WHERE ra.item_kind='route' AND r.pattern='/admin'")}
    assert acc.get("admin") == "allowed" and acc.get("alice") == "forbidden", acc
    assert q(run, "SELECT count(*) AS n FROM flows")[0]["n"] >= 1
    assert_outputs(run)


def test_ssr_approvals_held_and_executed_only_when_approved(ssr):
    run, log = ssr["run"], ssr["server"].log
    rows = q(run, "SELECT id, category, decision, executed_event, predicted_request "
                  "FROM approvals")
    assert any(r["category"] == "logout" and r["decision"] == "deny" for r in rows), rows
    approved = [r for r in rows if r["decision"] == "approve"]
    assert approved and all(r["category"] == "delete" for r in approved), rows
    assert ssr["applied"]["approved"] >= 1 and ssr["applied"]["denied"] >= 1, ssr["applied"]
    assert ssr["executed"].get("executed", 0) >= 1, ssr["executed"]
    # the server only ever saw deletes of approved (run-created) projects, never a logout
    approved_urls = {urlsplit(json.loads(r["predicted_request"] or "{}").get("url") or "").path
                     for r in approved}
    deletes = [e["path"] for e in log if e["method"] == "POST" and e["path"].endswith("/delete")]
    assert deletes and set(deletes) <= approved_urls, (deletes, approved_urls)
    assert all(int(p.split("/")[2]) >= 26 for p in deletes), deletes  # 1..25 pre-exist
    assert not [e for e in log if e["path"] == "/logout"], "logout was requested"
    assert not [e for e in log if e["method"] == "POST" and e["path"] == "/account/password"]
    # pre-existing projects' deletes stay held (pending), never sent
    assert any(r["category"] == "delete" and r["decision"] == "pending" for r in rows), rows


def assert_no_fixture_controls(server: Instrumented) -> None:
    """The workflow never hit the fixtures' test controls. The test itself makes no setup
    calls (each module starts a fresh app instance), so every logged request is the CLI's."""
    assert not [e for e in server.log if e["path"] in ("/__reset", "/__expire")], [
        e for e in server.log if e["path"].startswith("/__")]


def test_ssr_no_secrets_and_scope(ssr):
    assert_no_secrets(ssr["run"], ssr["server"].secrets)
    assert_hosts_in_scope(ssr["run"])
    assert_no_fixture_controls(ssr["server"])


# -- SPA -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def spa(tmp_path_factory):
    server = Instrumented("spa")
    try:
        roles = [{"name": n, "login": {"type": "form", "url": f"{server.url}/",
                                       "username_env": f"APPMAP_{n.upper()}_USER",
                                       "password_env": f"APPMAP_{n.upper()}_PASS",
                                       "success_selector": "text=Signed in as"}}
                 for n in ("admin", "alice")] + [{"name": "anonymous",
                                                  "login": {"type": "none"}}]
        created_sql = ("SELECT DISTINCT json_extract(resp_body, '$.id') AS id FROM exchanges "
                       "WHERE method='POST' AND url LIKE '%/api/items' AND status=201")
        res = workflow(server, tmp_path_factory.mktemp("spa"), roles, created_sql)
        res["server"] = server
        yield res
    finally:
        server.stop()


def test_spa_routes_endpoints_links_locators(spa):
    run = spa["run"]
    spa_routes = {r["pattern"] for r in q(run, "SELECT pattern FROM routes WHERE kind='spa_route'")}
    want = {"/", "/items", "/items/{id}", "/settings", "/admin"}
    assert want <= spa_routes, want - spa_routes
    eps = q(run, "SELECT method, path_pattern, kind, graphql_op FROM endpoints")
    ops = {e["graphql_op"] for e in eps if e["kind"] == "graphql_op"}
    assert {"ListItems", "DeleteItem"} <= ops, ops
    assert any(e["kind"] == "sse" and e["path_pattern"] == "/api/events" for e in eps), eps
    assert any(e["path_pattern"] == "/api/internal/flags" for e in eps), eps
    links = q(run, "SELECT p.method AS pm, p.url AS pu, c.method AS cm, c.url AS cu "
                   "FROM value_links v JOIN exchanges p ON p.id=v.producer_exchange_id "
                   "JOIN exchanges c ON c.id=v.consumer_exchange_id")
    assert any(l["pm"] == "POST" and urlsplit(l["pu"]).path == "/api/items"
               and l["cm"] == "GET" and re.fullmatch(r"/api/items/\d+", urlsplit(l["cu"]).path)
               for l in links), links[:20]
    missing = q(run, "SELECT t.id, t.action_type, t.action_label FROM transitions t "
                     "WHERE NOT EXISTS (SELECT 1 FROM locators l "
                     "WHERE l.locator_set_id = t.locator_set_id)")
    assert q(run, "SELECT count(*) AS n FROM transitions")[0]["n"] > 0
    assert not missing, missing
    assert_outputs(run)


def test_spa_delete_held_then_executed_only_if_approved(spa):
    run, log = spa["run"], spa["server"].log
    rows = q(run, "SELECT id, category, label, decision, executed_event, action_path, "
                  "predicted_request FROM approvals")
    assert any(r["category"] == "logout" and r["decision"] == "deny" for r in rows), rows
    assert any(r["category"] == "delete" and r["label"] == "Delete" for r in rows), rows
    approved = [r for r in rows if r["decision"] == "approve"]
    assert all(r["category"] == "delete" for r in approved), approved

    def item_id(r) -> int | None:  # the item an approved Delete acts on
        m = re.search(r"/items/(\d+)", f"{r['action_path']} {r['predicted_request'] or ''}")
        return int(m.group(1)) if m else None

    executed_ids = {item_id(r) for r in approved if r["executed_event"]} - {None}
    sent = [(json.loads(e["body"]).get("variables") or {}).get("id") for e in log
            if e["method"] == "POST" and e["path"] == "/graphql" and "DeleteItem" in e["body"]]
    # every DeleteItem the server saw targets an approved, executed item (subset check)
    assert set(sent) <= executed_ids, (sent, executed_ids)
    assert all(i > 100 for i in executed_ids), executed_ids  # 1..100 are seeded
    assert not [e for e in log if e["method"] == "DELETE"]


def test_spa_no_secrets_and_scope(spa):
    assert_no_secrets(spa["run"], spa["server"].secrets)
    assert_hosts_in_scope(spa["run"])
    assert_no_fixture_controls(spa["server"])


# -- the wrapper ---------------------------------------------------------------------
_OFFLINE = re.compile(r"Could not resolve|Temporary failure in name resolution|"
                      r"Name or service not known|nodename nor servname|Network is unreachable|"
                      r"Connection (refused|reset)|NewConnectionError|"
                      r"No matching distribution found", re.I)


def test_wrapper_version():
    """R3 smoke test of scripts/appmap. With the cached venv present it must answer quickly;
    building it (network, minutes) only runs when APPMAP_E2E_WRAPPER is set."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    venv = cache / "absolute-appmap" / "0.1.0" / "venv" / "bin" / "appmap"
    installed = venv.exists()
    if not installed and not os.environ.get("APPMAP_E2E_WRAPPER"):
        pytest.skip(f"{venv} missing; set APPMAP_E2E_WRAPPER=1 to test the full install")
    t0 = time.monotonic()
    proc = subprocess.run([str(PLUGIN / "scripts" / "appmap"), "--version"],
                          capture_output=True, text=True, timeout=60 if installed else 900)
    if proc.returncode != 0 and not installed and _OFFLINE.search(proc.stderr):
        pytest.skip(f"offline, cannot install: {proc.stderr[-300:]}")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "appmap 0.1.0"
    if installed:
        assert time.monotonic() - t0 < 20
