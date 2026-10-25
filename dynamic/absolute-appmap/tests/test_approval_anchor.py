"""Field fix 3: approvals of list-row controls whose only unique locator is positional
(``li:nth-of-type(N)``) - content anchors, 'predicted request not observed' failures and
de-duplication of re-queued items."""
import json
import re

import pytest
import werkzeug
import yaml
from werkzeug.wrappers import Request

from appmap.approvals import ApprovalQueue, PendingAction
from appmap.crawl import run_crawl
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}
_DEL = re.compile(r"^/items/(\d+)/delete$")


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _app(items: list[int], *, with_ids: bool = True):
    """A list of rows, each with an identical delete button (JS POST, then reload)."""

    @Request.application
    def app(request):
        m = _DEL.match(request.path)
        if m and request.method == "POST":
            n = int(m.group(1))
            if n in items:
                items.remove(n)
            return werkzeug.Response("ok")
        if request.path == "/":
            rows = "".join(
                f"<li{f' data-item-id={n}' if with_ids else ''}><span>Item {n}</span>"
                f"<button onclick=\"fetch('/items/{n}/delete',{{method:'POST'}})"
                f".then(()=>location.reload())\">x</button></li>" for n in items)
            return werkzeug.Response(
                f"<html><head><title>Items</title></head><body><ul id='list'>{rows}</ul>"
                "</body></html>", mimetype="text/html")
        return werkzeug.Response("nf", status=404)

    return app


def q(rd, sql, *args):
    store = Store(rd.db_path)
    try:
        return [dict(r) for r in store.conn.execute(sql, args)]
    finally:
        store.close()


def _approve_all(rd):
    store = Store(rd.db_path)
    try:
        queue = ApprovalQueue(store, rd.approvals_path)
        queue.write_yaml()
        data = yaml.safe_load(rd.approvals_path.read_text())
        for item in data["items"]:
            if item["decision"] == "pending":
                item["decision"] = "approve"
        rd.approvals_path.write_text(yaml.safe_dump(data, sort_keys=False))
    finally:
        store.close()


def _deleted(srv) -> list[int]:
    return [int(_DEL.match(p).group(1)) for m, p in srv.requests if m == "POST" and _DEL.match(p)]


def _by_item(rd) -> dict[int, dict]:
    out = {}
    for r in q(rd, "SELECT * FROM approvals"):
        pr = json.loads(r["predicted_request"] or "{}")
        m = re.search(r"/items/(\d+)/delete", pr.get("url") or "")
        if m:
            out[int(m.group(1))] = r
    return out


async def _crawl(srv, make_cfg, run_dir, tmp_path, browser):
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   limits=FAST)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    return cfg, rd


async def test_positional_row_controls_carry_a_content_anchor(browser, stub_server, make_cfg,
                                                              run_dir, tmp_path):
    items = [1, 2, 3, 4]
    srv = stub_server(_app(items))
    cfg, rd = await _crawl(srv, make_cfg, run_dir, tmp_path, browser)
    held = _by_item(rd)
    assert set(held) == {1, 2, 3} and not _deleted(srv)
    locs = json.loads(held[2]["action_path"])[-1]["locators"]
    anchor = [l for l in locs if l["strategy"] == "anchor"]
    assert anchor and json.loads(anchor[0]["value"])["attr"] == ["data-item-id", "2"]

    _approve_all(rd)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2,
                            approved_only=True, browser=browser)
    # each approval deletes its own row although the list shifted after every delete
    assert sorted(_deleted(srv)) == [1, 2, 3], srv.requests
    assert stats.approvals["executed"] == 3 and stats.approvals["failed"] == 0
    assert items == [4]


async def test_moved_target_fails_and_needs_reapproval(browser, stub_server, make_cfg,
                                                       run_dir, tmp_path):
    """Without an anchor (old approval rows), a shifted list makes the positional click
    hit another row: its request is blocked, and the item is failed - not executed - and
    back to pending until the human approves it again."""
    items = [1, 2, 3, 4]
    srv = stub_server(_app(items, with_ids=False))
    cfg, rd = await _crawl(srv, make_cfg, run_dir, tmp_path, browser)
    held = _by_item(rd)
    store = Store(rd.db_path)
    try:  # strip anchors: the approval only knows li:nth-of-type(2)
        path = json.loads(held[2]["action_path"])
        path[-1]["locators"] = [l for l in path[-1]["locators"] if l["strategy"] != "anchor"]
        store.conn.execute("UPDATE approvals SET action_path=? WHERE id=?",
                           (json.dumps(path), held[2]["id"]))
    finally:
        store.close()
    items.remove(1)  # the list shifts out of band: row 2 now holds item 3
    store = Store(rd.db_path)
    try:
        ApprovalQueue(store, rd.approvals_path).write_yaml()
    finally:
        store.close()
    data = yaml.safe_load(rd.approvals_path.read_text())
    for it in data["items"]:
        if it["id"] == held[2]["id"]:
            it["decision"] = "approve"
    rd.approvals_path.write_text(yaml.safe_dump(data, sort_keys=False))

    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2,
                            approved_only=True, browser=browser)
    assert _deleted(srv) == []  # item 3's delete was blocked, item 2 untouched
    assert stats.approvals["failed"] == 1 and stats.approvals["executed"] == 0
    row = q(rd, "SELECT decision, executed_event FROM approvals WHERE id=?", held[2]["id"])[0]
    assert row == {"decision": "pending", "executed_event": None}
    assert any("predicted request not observed" in w for w in stats.warnings)
    ya = {i["id"]: i for i in yaml.safe_load(rd.approvals_path.read_text())["items"]}
    assert ya[held[2]["id"]]["decision"] == "pending"
    assert "predicted request not observed" in ya[held[2]["id"]]["execution"]
    # a rule does not re-approve it; the human re-approving does
    data = yaml.safe_load(rd.approvals_path.read_text())
    data["rules"] = [{"decision": "approve", "match": {"category": "delete"}}]
    rd.approvals_path.write_text(yaml.safe_dump(data, sort_keys=False))
    store = Store(rd.db_path)
    try:
        queue = ApprovalQueue(store, rd.approvals_path)
        queue.load_decisions()
        assert q(rd, "SELECT decision FROM approvals WHERE id=?",
                 held[2]["id"])[0]["decision"] == "pending"
    finally:
        store.close()


async def test_anchor_gone_fails_without_clicking(browser, stub_server, make_cfg, run_dir,
                                                  tmp_path):
    items = [1, 2, 3, 4]
    srv = stub_server(_app(items))
    cfg, rd = await _crawl(srv, make_cfg, run_dir, tmp_path, browser)
    held = _by_item(rd)
    items.remove(2)  # its row is gone; position 2 now holds item 3
    data = None
    _approve_all(rd)
    data = yaml.safe_load(rd.approvals_path.read_text())
    for it in data["items"]:
        if it["id"] != held[2]["id"]:
            it["decision"] = "deny"
    rd.approvals_path.write_text(yaml.safe_dump(data, sort_keys=False))
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2,
                            approved_only=True, browser=browser)
    assert _deleted(srv) == []
    assert stats.approvals["failed"] == 1
    assert q(rd, "SELECT decision, executed_event FROM approvals WHERE id=?",
             held[2]["id"])[0] == {"decision": "pending", "executed_event": None}


def test_requeued_destructive_item_dedupes_on_predicted_request(tmp_path):
    store = Store(tmp_path / "a.sqlite")
    store.init_schema()
    queue = ApprovalQueue(store, tmp_path / "approvals.yaml")

    def pa(nth, url, method="POST", href=None):
        step = {"type": "click", "label": "x",
                "locators": [{"strategy": "css", "value": f"li:nth-of-type({nth}) > button"}]}
        if href:
            step["href"] = href
        return PendingAction(id="", role="user", state_id=None, category="delete", label="x",
                             action_path=[{"type": "goto", "url": "http://h/"}, step],
                             predicted_request={"method": method, "url": url})

    a = queue.enqueue(pa(2, "http://h/items/5/delete"))
    store.upsert_approval(a, decision="approve", executed_event="ev-1")
    assert queue.enqueue(pa(1, "http://h/items/5/delete/")) == a  # list shifted, same target
    assert queue.enqueue(pa(1, "http://h/items/6/delete")) != a
    # a link to the same GET target from another position is the same action too
    g = queue.enqueue(pa(3, "http://h/t/9/task_delete", "GET", href="http://h/t/9/task_delete"))
    assert queue.enqueue(pa(1, "http://h/t/9/task_delete", "GET",
                            href="http://h/t/9/task_delete")) == g
    # but a GET that is only the page the control sits on is not a target
    p1 = queue.enqueue(pa(1, "http://h/list", "GET"))
    assert queue.enqueue(pa(2, "http://h/list", "GET")) != p1
    # another role keeps its own item
    other = pa(2, "http://h/items/5/delete")
    other.role = "admin"
    assert queue.enqueue(other) != a
    store.close()
