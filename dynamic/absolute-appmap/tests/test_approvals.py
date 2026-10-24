import yaml

from appmap.approvals import ApprovalQueue, PendingAction
from appmap.store import Store


def _pa(label="Delete x", url="http://t/api/appmap-1", role="admin", cat="delete", path=None):
    return PendingAction(id="", role=role, state_id="s1", category=cat, label=label,
                         reasons=["r"], action_path=path or [{"op": "click", "label": label}],
                         predicted_request={"method": "DELETE", "url": url})


def _q(tmp_path):
    s = Store(tmp_path / "s.db")
    s.init_schema()
    return ApprovalQueue(s, tmp_path / "approvals.yaml")


def _decision(q, id):
    return q.store.conn.execute("SELECT decision FROM approvals WHERE id=?", (id,)).fetchone()[0]


def test_enqueue_dedups(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa())
    assert a == "ap-0001"
    assert q.enqueue(_pa()) == a
    assert q.enqueue(_pa(label="Delete y", path=[{"op": "click", "label": "y"}])) == "ap-0002"
    assert q.enqueue(_pa(role="user")) == "ap-0003"
    assert q.counts()["pending"] == 3


def test_ids_continue_from_store(tmp_path):
    q = _q(tmp_path)
    q.store.upsert_approval("ap-0007", role="x", action_path=[])
    assert q.enqueue(_pa()) == "ap-0008"


def test_rule_approves_marker_deletes(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa(url="http://t/api/appmap-1"))
    b = q.enqueue(_pa(label="Delete other", url="http://t/api/real", path=[{"o": 1}]))
    q.write_yaml()
    data = yaml.safe_load(q.yaml_path.read_text())
    assert [i["id"] for i in data["items"]] == [a, b]
    data["rules"] = [{"match": {"category": "delete", "url_contains": "appmap-"}, "decision": "approve"}]
    q.yaml_path.write_text(yaml.safe_dump(data))
    assert q.load_decisions() == 1
    assert _decision(q, a) == "approve"
    assert _decision(q, b) == "pending"
    assert [p.id for p in q.approved_unexecuted()] == [a]
    q.mark_executed(a, "ev1")
    assert q.approved_unexecuted() == []
    assert q.counts() == {"pending": 1, "approve": 1, "deny": 0, "skip": 0, "executed": 1}


def test_item_decision_beats_rule(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa())
    q.write_yaml()
    data = yaml.safe_load(q.yaml_path.read_text())
    data["items"][0]["decision"] = "deny"
    data["items"][0]["note"] = "no"
    data["rules"] = [{"match": {"category": "delete"}, "decision": "approve"}]
    q.yaml_path.write_text(yaml.safe_dump(data))
    assert q.load_decisions() == 1
    assert _decision(q, a) == "deny"
    assert q.load_decisions() == 0
    q.write_yaml()  # note preserved
    assert yaml.safe_load(q.yaml_path.read_text())["items"][0]["note"] == "no"


def test_load_without_file(tmp_path):
    assert _q(tmp_path).load_decisions() == 0
