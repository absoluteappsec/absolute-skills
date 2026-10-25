from appmap.frontier import Frontier, FrontierItem
from appmap.store import Store


def _frontier(tmp_path):
    store = Store(tmp_path / "f.sqlite")
    store.init_schema()
    return Frontier(store), store


def test_push_dedups_url_and_action_items(tmp_path):
    f, _ = _frontier(tmp_path)
    assert f.push("admin", "url", url="http://x/a", depth=0, source="ui")
    assert not f.push("admin", "url", url="http://x/a", depth=3, source="sitemap")
    assert f.push("alice", "url", url="http://x/a", depth=0, source="ui")  # per role
    path = [{"type": "goto", "url": "http://x/a"},
            {"type": "click", "locators": [{"strategy": "text", "value": "Go", "rank": 0}],
             "label": "Go"}]
    assert f.push("admin", "action", url="http://x/a", action_path=path, depth=1, source="ui")
    assert not f.push("admin", "action", url="http://x/a", action_path=[dict(s) for s in path],
                      depth=1, source="ui")
    assert f.pending_count() == 3
    assert f.pending_count("admin") == 2


def test_pop_orders_by_priority_then_depth_and_mark(tmp_path):
    f, store = _frontier(tmp_path)
    f.push("r", "url", url="http://x/deep", depth=4, source="ui")
    f.push("r", "url", url="http://x/shallow", depth=1, source="ui")
    f.push("r", "url", url="http://x/urgent", depth=9, priority=5, source="ui")
    first = f.pop("r")
    assert isinstance(first, FrontierItem)
    assert first.url == "http://x/urgent" and first.action_path == [] and first.status == "pending"
    # pop does not consume: the caller marks the outcome (crash-safe resume)
    assert f.pop("r").id == first.id
    f.mark(first.id, "done")
    assert f.pop("r").url == "http://x/shallow"
    f.mark(f.pop("r").id, "failed")
    assert f.pop("r").url == "http://x/deep"
    f.mark(f.pop("r").id, "queued_approval")
    assert f.pop("r") is None and f.pending_count("r") == 0
    statuses = {r["url"]: r["status"] for r in store.conn.execute("SELECT url, status FROM frontier")}
    assert statuses == {"http://x/urgent": "done", "http://x/shallow": "failed",
                        "http://x/deep": "queued_approval"}


def test_action_path_round_trips(tmp_path):
    f, store = _frontier(tmp_path)
    path = [{"type": "goto", "url": "http://x/"},
            {"type": "submit", "form_label": "Create", "locators": []}]
    f.push("r", "action", action_path=path, depth=2, source="ui")
    item = f.pop("r")
    assert item.kind == "action" and item.action_path == path and item.depth == 2
    assert item.url == ""  # stored non-NULL (ruling R6)
    assert store.conn.execute("SELECT url FROM frontier").fetchone()[0] == ""


def test_set_action_path_clash_keeps_existing_row(tmp_path, caplog):
    f, store = _frontier(tmp_path)
    base = [{"type": "goto", "url": "http://x/"}, {"type": "click", "locators": [], "label": "S"}]
    marked = [base[0], {**base[1], "mutating": True}]
    f.push("r", "action", action_path=base, depth=1, source="ui")
    f.push("r", "action", action_path=marked, depth=1, source="ui")
    first = f.id_of("r", "action", action_path=base)
    assert f.set_action_path(first, marked) is False
    assert "already exists" in caplog.text
    other = f.push("r", "action", action_path=[base[0]], depth=0, source="ui")
    assert other and f.set_action_path(f.id_of("r", "action", action_path=[base[0]]),
                                       [{**base[0], "mutating": True}]) is True
