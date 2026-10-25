"""Append-only JSONL event log, the backbone for reconstructing flows."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from appmap.redact import Redactor

EVENT_TYPES = frozenset({
    "navigate", "click", "fill", "submit", "request", "response", "dialog",
    "state_entered", "approval_queued", "approval_started", "approval_executed", "login",
    "session_lost", "websocket", "sse", "error", "observation",
})


@dataclass
class Event:
    id: str
    ts: float
    type: str
    role: str | None
    caused_by: str | None
    data: dict = field(default_factory=dict)


def _parse(line: str) -> Event | None:
    try:
        obj = json.loads(line)
        return Event(**obj)
    except (ValueError, TypeError):
        return None


class EventLog:
    def __init__(self, path: Path, redactor: Redactor):
        self.path = Path(path)
        self._redactor = redactor
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._truncate_partial_line()
        self._last = 0
        for ev in self.iter():
            try:
                self._last = max(self._last, int(ev.id.split("-")[1]))
            except (IndexError, ValueError):
                pass

    def _truncate_partial_line(self) -> None:
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            with open(self.path, "r+b") as f:
                f.truncate(raw.rfind(b"\n") + 1)

    def emit(self, type: str, *, role: str | None = None,
             caused_by: str | None = None, **data) -> Event:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type}")
        with self._lock:
            self._last += 1
            ev = Event(id=f"ev-{self._last:06d}", ts=time.time(), type=type, role=role,
                       caused_by=caused_by, data=self._redactor.json(data))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(ev), ensure_ascii=False, default=str) + "\n")
        return ev

    def iter(self) -> Iterator[Event]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip() and (ev := _parse(line)) is not None:
                    yield ev
