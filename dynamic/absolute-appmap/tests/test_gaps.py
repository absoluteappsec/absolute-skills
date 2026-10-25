"""Task 18: coverage gaps, pass tracking and ``appmap status``."""
import json

from appmap.cli import main
from appmap.gaps import compute_gaps
from appmap.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "appmap.sqlite")
    s.init_schema()
    return s


def test_gaps_lists_schema_only_endpoint(tmp_path):
    s = _store(tmp_path)
    schema_only = s.upsert_endpoint("GET", "h", "/api/ghost", "rest")
    s.upsert_source("endpoint", schema_only, "openapi")
    seen = s.upsert_endpoint("GET", "h", "/api/seen", "rest")
    s.upsert_source("endpoint", seen, "openapi")
    s.add_exchange(seen, "admin", "ui")
    ui = s.upsert_endpoint("GET", "h", "/api/ui", "rest")
    s.upsert_source("endpoint", ui, "ui")  # a non-discovery source, even if unexercised
    # a route reached by a state is not a gap; a bare spa route is
    s.upsert_route("/home", "h", "page")
    s.upsert_state("s1", "admin", "http://h/home", route_pattern="/home")
    s.upsert_route("/secret", "h", "spa_route")
    # frontier / approvals
    s.conn.execute("INSERT INTO frontier(role,kind,url,status) VALUES('admin','url','/a','pending')")
    s.conn.execute("INSERT INTO frontier(role,kind,url,status) VALUES('admin','url','/b','failed')")
    s.conn.execute("INSERT INTO frontier(role,kind,url,status) VALUES('admin','url','/c','done')")
    s.upsert_approval("p1", role="admin", label="Delete", reasons=["x"])
    s.upsert_approval("d1", role="admin", label="Wipe", decision="deny")
    g = compute_gaps(s)
    assert [e["path_pattern"] for e in g.unexercised_endpoints] == ["/api/ghost"]
    assert g.unexercised_endpoints[0]["sources"] == ["openapi"]
    assert [r["pattern"] for r in g.unreached_routes] == ["/secret"]
    assert [a["url"] for a in g.unexplored_actions] == ["/a", "/b"]
    assert [a["id"] for a in g.pending_approvals] == ["p1"]
    assert [a["id"] for a in g.denied_approvals] == ["d1"]


def test_failed_form_listed(tmp_path):
    s = _store(tmp_path)
    s.add_form("s1", method="POST", purpose="login", succeeded=False)
    s.add_form("s1", method="POST", purpose="ok", succeeded=True)
    forms = compute_gaps(s).unsuccessful_forms
    assert [f["purpose"] for f in forms] == ["login"] and forms[0]["attempted"] is True


def test_approved_failure_reason_listed(tmp_path):
    s = _store(tmp_path)
    s.upsert_approval("a1", role="admin", label="Do", decision="approve")
    s.set_meta("approval_failed:a1", "no session")
    assert compute_gaps(s).unexplored_actions[0]["reason"] == "no session"


def _run(make_cfg, tmp_path, capsys):
    cfg = make_cfg("http://127.0.0.1:1")
    out = tmp_path / "runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    capsys.readouterr()
    return next(out.iterdir())


def _status(capsys, *argv):
    rc = main(["status", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    return rc, lines[:-1], json.loads(lines[-1].removeprefix("APPMAP_STATUS "))


def test_new_in_pass_zero_when_nothing_new(make_cfg, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = _run(make_cfg, tmp_path, capsys)
    s = Store(root / "appmap.sqlite")
    s.upsert_state("s1", "admin", "http://h/", route_pattern="/")
    s.upsert_endpoint("GET", "h", "/x", "rest")
    s.close()
    rc, _, st = _status(capsys, "--run", str(root))
    assert rc == 0 and st["counts"]["pass"] == 1
    assert st["counts"]["new_in_pass"] == {"states": 1, "endpoints": 1, "routes": 0}
    assert st["next"].startswith("appmap build")
    rc, _, st = _status(capsys, "--run", str(root), "--new-pass")
    assert st["counts"]["pass"] == 2
    assert st["counts"]["new_in_pass"] == {"states": 0, "endpoints": 0, "routes": 0}
    s = Store(root / "appmap.sqlite")
    s.upsert_state("s2", "admin", "http://h/b", route_pattern="/b")
    s.conn.execute("INSERT INTO frontier(role,kind,url) VALUES('admin','url','/q')")
    s.close()
    _, _, st = _status(capsys, "--run", str(root))
    assert st["counts"]["new_in_pass"]["states"] == 1
    assert st["counts"]["pending_frontier"] == 1 and st["next"].startswith("appmap crawl")


def test_status_gaps_line_precedes_status(make_cfg, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = _run(make_cfg, tmp_path, capsys)
    s = Store(root / "appmap.sqlite")
    s.upsert_approval("p1", role="a", label="L")
    s.close()
    _, pre, st = _status(capsys, "--run", str(root), "--gaps")
    gaps = json.loads(pre[-1])["gaps"]
    assert [a["id"] for a in gaps["pending_approvals"]] == ["p1"]
    assert st["counts"]["gaps"]["pending_approvals"] == 1
    assert st["pending_approvals"] == 1 and "approvals" in st["next"] and "write" in st["next"]
