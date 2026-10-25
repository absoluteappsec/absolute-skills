"""Persistent crawl frontier (the ``frontier`` table) shared by every discovery source.

``url`` items are deduplicated by URL; ``action`` items by their full ``action_path``.
Item shapes in ``action_path``:
  {"type": "goto", "url": str}
  {"type": "click", "locators": [...], "label": str}
  {"type": "submit", "form_label": str, "locators": [...]}
(extra descriptive keys are allowed). ``pop`` does not consume: callers ``mark`` the
outcome, so an item interrupted by a crash is retried on resume.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .store import Store

STATUSES = ("pending", "done", "skipped", "queued_approval", "failed")


@dataclass
class FrontierItem:
    id: int
    role: str
    kind: str
    url: str
    action_path: list[dict] = field(default_factory=list)
    depth: int = 0
    priority: int = 0
    status: str = "pending"
    source: str | None = None


def _path_json(action_path: list[dict] | None) -> str:
    return json.dumps(action_path or [], sort_keys=True)


class Frontier:
    def __init__(self, store: Store):
        self.store = store

    def push(self, role: str, kind: str, *, url: str | None = None,
             action_path: list[dict] | None = None, depth: int, priority: int = 0,
             source: str) -> bool:
        """Add an item; False if an identical (role, kind, url, action_path) exists."""
        cur = self.store.conn.execute(
            "INSERT OR IGNORE INTO frontier(role,kind,url,action_path,depth,priority,status,source) "
            "VALUES(?,?,?,?,?,?,'pending',?)",
            (role, kind, url or "", _path_json(action_path), depth, priority, source))
        return cur.rowcount == 1

    def id_of(self, role: str, kind: str, *, url: str | None = None,
              action_path: list[dict] | None = None) -> int | None:
        row = self.store.conn.execute(
            "SELECT id FROM frontier WHERE role=? AND kind=? AND url=? AND action_path=?",
            (role, kind, url or "", _path_json(action_path))).fetchone()
        return None if row is None else int(row[0])

    def pop(self, role: str) -> FrontierItem | None:
        """Highest priority, then lowest depth, then oldest pending item for ``role``."""
        row = self.store.conn.execute(
            "SELECT * FROM frontier WHERE role=? AND status='pending' "
            "ORDER BY priority DESC, depth ASC, id ASC LIMIT 1", (role,)).fetchone()
        if row is None:
            return None
        return FrontierItem(id=row["id"], role=row["role"], kind=row["kind"], url=row["url"],
                            action_path=json.loads(row["action_path"] or "[]"),
                            depth=row["depth"], priority=row["priority"],
                            status=row["status"], source=row["source"])

    def mark(self, item_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown frontier status: {status}")
        self.store.conn.execute("UPDATE frontier SET status=? WHERE id=?", (status, item_id))

    # -- link provenance (frontier_links) -----------------------------------------
    def add_link(self, role: str, url: str, from_state: str, label: str,
                 locators: list[dict]) -> int | None:
        """Remember that ``from_state`` links to ``url``; returns the row id if new."""
        cur = self.store.conn.execute(
            "INSERT OR IGNORE INTO frontier_links(role,url,from_state,label,locators) "
            "VALUES(?,?,?,?,?)", (role, url, from_state, label or "", json.dumps(locators)))
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def visited_state(self, role: str, url: str) -> str | None:
        row = self.store.conn.execute(
            "SELECT to_state FROM frontier_links WHERE role=? AND url=? AND from_state=''",
            (role, url)).fetchone()
        return None if row is None else row[0]

    def mark_visited(self, role: str, url: str, state: str) -> list[tuple[int, str, str, list]]:
        """Record where ``url`` landed; return pending links (id, from_state, label, locators)."""
        self.store.conn.execute(
            "INSERT INTO frontier_links(role,url,from_state,label,to_state) VALUES(?,?,'','',?) "
            "ON CONFLICT(role,url,from_state,label) DO UPDATE SET to_state=excluded.to_state",
            (role, url, state))
        rows = self.store.conn.execute(
            "SELECT id, from_state, label, locators FROM frontier_links "
            "WHERE role=? AND url=? AND from_state!='' AND to_state IS NULL", (role, url))
        return [(r[0], r[1], r[2], json.loads(r[3] or "[]")) for r in rows]

    def resolve_link(self, link_id: int, to_state: str) -> None:
        self.store.conn.execute("UPDATE frontier_links SET to_state=? WHERE id=?",
                                (to_state, link_id))

    def status_of(self, item_id: int) -> str | None:
        row = self.store.conn.execute("SELECT status FROM frontier WHERE id=?", (item_id,)).fetchone()
        return None if row is None else row[0]

    def set_action_path(self, item_id: int, action_path: list[dict]) -> None:
        """Annotate an item's path (e.g. ``mutating`` steps); identity is not re-checked."""
        self.store.conn.execute("UPDATE OR IGNORE frontier SET action_path=? WHERE id=?",
                                (_path_json(action_path), item_id))

    def pending_count(self, role: str | None = None) -> int:
        if role is None:
            sql, args = "SELECT count(*) FROM frontier WHERE status='pending'", ()
        else:
            sql, args = "SELECT count(*) FROM frontier WHERE status='pending' AND role=?", (role,)
        return int(self.store.conn.execute(sql, args).fetchone()[0])
