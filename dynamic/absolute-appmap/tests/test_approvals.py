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


def _write(q, mutate):
    q.write_yaml()
    data = yaml.safe_load(q.yaml_path.read_text())
    mutate(data)
    q.yaml_path.write_text(yaml.safe_dump(data))


def test_invalid_rules_not_applied_and_reported(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa())
    bad = [{"match": {}, "decision": "approve"},
           {"match": {"categroy": "delete"}, "decision": "approve"},
           {"match": {"category": "delete", "typo": "x"}, "decision": "approve"},
           {"match": {"label_contains": ""}, "decision": "approve"},
           {"match": {"category": "delete"}, "decision": "aprove"},
           {"decision": "approve"}, "junk"]
    _write(q, lambda d: d.update(rules=bad))
    assert q.load_decisions() == 0
    assert _decision(q, a) == "pending"
    assert len(q.warnings) == len(bad)


def test_role_and_label_rules(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa(role="admin", label="Delete Foo", path=[{"o": 1}]))
    b = q.enqueue(_pa(role="user", label="Delete Bar", path=[{"o": 2}]))
    c = q.enqueue(_pa(role="user", label="Other", path=[{"o": 3}]))
    _write(q, lambda d: d.update(rules=[
        {"match": {"role": "admin"}, "decision": "skip"},
        {"match": {"role": "user", "label_contains": "delete b"}, "decision": "approve"}]))
    assert q.load_decisions() == 2
    assert (_decision(q, a), _decision(q, b), _decision(q, c)) == ("skip", "approve", "pending")
    assert q.warnings == []


def test_url_contains_case_insensitive(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa(url="http://t/api/APPMAP-9"))
    _write(q, lambda d: d.update(rules=[{"match": {"url_contains": "appmap-"}, "decision": "approve"}]))
    assert q.load_decisions() == 1 and _decision(q, a) == "approve"


def test_dedup_distinguishes_requests_with_empty_paths(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa(url="http://t/a/1", path=[]))
    b = q.enqueue(_pa(url="http://t/a/2", path=[]))
    assert a != b
    assert q.enqueue(_pa(url="http://t/a/1", path=[])) == a


def test_pending_revokes_and_typos_warn(tmp_path):
    q = _q(tmp_path)
    a = q.enqueue(_pa())
    b = q.enqueue(_pa(label="Delete y", path=[{"o": 1}]))
    _write(q, lambda d: (d["items"][0].update(decision="approve"), d["items"][1].update(decision="aprove")))
    assert q.load_decisions() == 1 and _decision(q, a) == "approve"
    assert _decision(q, b) == "pending" and len(q.warnings) == 1
    _write(q, lambda d: d["items"][0].update(decision="pending"))
    assert q.load_decisions() == 1
    row = q.store.conn.execute("SELECT decision, decided_by FROM approvals WHERE id=?", (a,)).fetchone()
    assert tuple(row) == ("pending", None)
