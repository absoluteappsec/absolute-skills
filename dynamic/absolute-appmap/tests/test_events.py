from appmap.events import EventLog
from appmap.redact import Redactor


def test_event_ids_continue_after_reopen(tmp_path):
    p = tmp_path / "events.jsonl"
    log = EventLog(p, Redactor([], []))
    assert log.emit("navigate", url="http://x").id == "ev-000001"
    assert log.emit("click", role="user", caused_by="ev-000001").id == "ev-000002"
    log2 = EventLog(p, Redactor([], []))
    assert log2.emit("fill").id == "ev-000003"
    evs = list(log2.iter())
    assert [e.id for e in evs] == ["ev-000001", "ev-000002", "ev-000003"]
    assert evs[1].role == "user" and evs[1].caused_by == "ev-000001"
    assert evs[0].data == {"url": "http://x"}


def test_truncated_last_line_tolerated(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"id":"ev-000001","ts":1,"type":"navigate","role":null,"caused_by":null,"data":{}}\n{"id":"ev-0000')
    log = EventLog(p, Redactor([], []))
    assert [e.id for e in log.iter()] == ["ev-000001"]
    assert log.emit("click").id == "ev-000002"
    assert [e.id for e in EventLog(p, Redactor([], [])).iter()] == ["ev-000001", "ev-000002"]


def test_emit_redacts_data(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", Redactor(["hunter2"], []))
    log.emit("request", headers={"Authorization": "Bearer abc"}, body="pw=hunter2",
             form={"password": "p", "q": "ok"})
    e = next(log.iter())
    assert e.data["headers"]["Authorization"] == "<redacted:authorization>"
    assert "hunter2" not in e.data["body"]
    assert e.data["form"] == {"password": "<redacted:password>", "q": "ok"}
    assert "hunter2" not in (tmp_path / "e.jsonl").read_text()


def test_unknown_event_type_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        EventLog(tmp_path / "e.jsonl", Redactor([], [])).emit("bogus")


def test_bytes_and_exception_data_not_leaked(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p, Redactor(["hunter2"], []))
    log.emit("error", body=b"pw=hunter2", error=Exception("Bearer abc"))
    text = p.read_text()
    assert "hunter2" not in text and "Bearer abc" not in text
