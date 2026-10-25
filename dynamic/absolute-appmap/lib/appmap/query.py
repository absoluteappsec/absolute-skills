"""Query recipes and read-only SQL over a run's ``appmap.sqlite`` (``appmap query``).

The recipe SQL is read from ``resources/SCHEMA.md`` (the "Query recipes" section and the
``flow-form-fields`` example) so the documentation and the implementation cannot drift.
Two recipes are implemented in Python and their SQL is kept in ``RECIPES`` as the
reference only: ``path-to-state`` (a bounded BFS over transitions, ruling R22) and ``gaps``
(reuses ``gaps.compute_gaps`` so it matches ``appmap status --gaps``). Both return the
same columns as the documented SQL.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from appmap.gaps import compute_gaps
from appmap.store import Store

SCHEMA_MD = Path(__file__).parent / "resources" / "SCHEMA.md"
STATEMENT_TIMEOUT_S = 10.0
MAX_PATH_DEPTH = 25


class QueryError(ValueError):
    """A rejected or failed query (bad recipe/params, non-read SQL, timeout, SQL error)."""


def _load_recipes() -> dict[str, str]:
    text = SCHEMA_MD.read_text()
    section = text.split("## Query recipes", 1)[1]
    recipes = dict(re.findall(r"### `([\w-]+)`\n.*?```sql\n(.*?)```", section, re.S))
    extra = re.search(r"`flow-form-fields`.*?```sql\n(.*?)```", text, re.S)
    if extra:
        recipes["flow-form-fields"] = extra.group(1)
    return recipes


RECIPES: dict[str, str] = _load_recipes()

# recipe -> (required params, optional params bound to NULL when absent)
RECIPE_PARAMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "flows": ((), ("role",)),
    "forms-posting-to": (("endpoint",), ()),
    "hidden-but-reachable": (("role",), ()),
    "endpoint-shape": (("method", "path"), ()),
    "path-to-state": (("state",), ()),
    "role-matrix": ((), ()),
    "gaps": ((), ()),
    "flow-form-fields": (("flow",), ()),
}
PYTHON_RECIPES = {"path-to-state", "gaps"}

# Authorizer: only reading is allowed (no PRAGMA, ATTACH, writes, DDL, transactions).
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE}


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _execute(store: Store, sql: str, params=()) -> list[dict]:
    conn = store.conn
    deadline = time.monotonic() + STATEMENT_TIMEOUT_S
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryError(f"query exceeded the {STATEMENT_TIMEOUT_S:g}s statement timeout")
        raise QueryError(str(e)) from e
    except sqlite3.Error as e:
        raise QueryError(str(e)) from e
    finally:
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)


_COMMENT = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)", re.S)


def _first_words(sql: str, n: int) -> list[str]:
    while (m := _COMMENT.match(sql)):
        sql = sql[m.end():]
    return re.findall(r"[A-Za-z_]+", sql)[:n]


def run_sql(store: Store, sql: str) -> list[dict]:
    """Run one read statement (SELECT / WITH / EXPLAIN QUERY PLAN) and return its rows."""
    words = [w.upper() for w in _first_words(sql, 3)]
    if not (words[:1] in (["SELECT"], ["WITH"]) or words == ["EXPLAIN", "QUERY", "PLAN"]):
        raise QueryError("only a single SELECT, WITH or EXPLAIN QUERY PLAN statement is allowed")
    try:
        return _execute(store, sql)
    except QueryError as e:
        if "one statement at a time" in str(e):
            raise QueryError("only a single statement is allowed") from e
        raise
    except sqlite3.ProgrammingError as e:  # pragma: no cover - older Python wording
        raise QueryError(str(e)) from e


# -- path-to-state (BFS, mirrors the SCHEMA.md SQL reference) -----------------------------
def _path_to_state(store: Store, state: str) -> list[dict]:
    q = store.conn.execute
    if q("SELECT 1 FROM states WHERE id=?", (state,)).fetchone() is None:
        return []
    edges = [dict(r) for r in q(
        "SELECT id, from_state, to_state, action_type, action_label, locator_set_id "
        "FROM transitions ORDER BY id")]
    incoming: dict[str, list[dict]] = {}
    for t in edges:
        if t["to_state"] is not None:
            incoming.setdefault(t["to_state"], []).append(t)
    # 1. BFS backwards over click/submit edges: one visit per state.
    dist = {state: 0}
    frontier = [state]
    while frontier and dist[frontier[0]] < MAX_PATH_DEPTH:
        nxt = []
        for s in frontier:
            for t in incoming.get(s, ()):
                f = t["from_state"]
                if t["action_type"] != "navigate" and f is not None and f not in dist:
                    dist[f] = dist[s] + 1
                    nxt.append(f)
        frontier = nxt
    # 2. Entry: nearest state loadable by URL (a navigate edge leads to it) or with no way in.
    def loadable(s):
        ins = incoming.get(s, ())
        return not ins or any(t["action_type"] == "navigate" for t in ins)
    entries = sorted((d, s) for s, d in dist.items() if loadable(s))
    if not entries:
        return []
    # 3. Walk forward, lowest transition id among edges one step closer to the target.
    out_edges: dict[str, list[dict]] = {}
    for t in edges:
        if t["from_state"] is not None and t["action_type"] != "navigate":
            out_edges.setdefault(t["from_state"], []).append(t)
    cur = entries[0][1]
    urls = {r["id"]: r["url"] for r in q("SELECT id, url FROM states")}
    rows = [{"step": 0, "action_type": "goto", "url": urls[cur], "label": None,
             "transition_id": None, "locator_set_id": None, "locators": None}]
    while dist[cur] > 0:
        step = next((t for t in out_edges.get(cur, ())
                     if dist.get(t["to_state"]) == dist[cur] - 1), None)
        if step is None:  # cannot happen: dist came from these same edges
            break
        locs = [dict(r) for r in q(
            "SELECT strategy, value, rank FROM locators WHERE locator_set_id=? ORDER BY rank",
            (step["locator_set_id"],))]
        rows.append({"step": len(rows), "action_type": step["action_type"],
                     "url": urls.get(cur), "label": step["action_label"],
                     "transition_id": step["id"], "locator_set_id": step["locator_set_id"],
                     "locators": json.dumps(locs, separators=(",", ":"))})
        cur = step["to_state"]
    return rows


# -- gaps (flattened compute_gaps, same columns as the SQL reference) ----------------------
def _gap_rows(store: Store) -> list[dict]:
    g = compute_gaps(store)
    rows = []

    def add(gap, id_, item, detail):
        rows.append({"gap": gap, "id": str(id_), "item": item, "detail": detail})

    for e in g.unexercised_endpoints:
        item = f"{e['method']} {e['path_pattern']}" + (
            f" {e['graphql_op']}" if e.get("graphql_op") else "")
        add("unexercised_endpoint", e["id"], item, ",".join(e["sources"]) or None)
    for f in g.unsuccessful_forms:
        add("unsuccessful_form", f["id"], f["state_id"],
            "failed" if f["attempted"] else "not attempted")
    for r in g.unreached_routes:
        add("unreached_route", r["id"], r["pattern"], r["kind"])
    for a in g.unexplored_actions:
        if a.get("kind") == "approval":
            add("approved_not_executed", a["id"], f"{a['role']} {a.get('label') or ''}",
                a.get("reason") or "approved_pending")
        else:
            what = a["url"] if a["kind"] == "url" else json.dumps(a["action_path"])
            add("unexplored_action", a["id"], f"{a['role']} {a['kind']} {what}", a["status"])
    for gap, items in (("pending_approval", g.pending_approvals),
                       ("denied_approval", g.denied_approvals)):
        for a in items:
            add(gap, a["id"], f"{a['role']} {a.get('category') or ''} {a.get('label') or ''}",
                json.dumps(a["reasons"]))
    rows.sort(key=lambda r: (r["gap"], r["id"]))
    return rows


def run_recipe(store: Store, name: str, **params) -> list[dict]:
    if name not in RECIPE_PARAMS or name not in RECIPES:
        raise QueryError(f"unknown recipe {name!r}; available: {', '.join(sorted(RECIPE_PARAMS))}")
    required, optional = RECIPE_PARAMS[name]
    missing = [p for p in required if params.get(p) is None]
    if missing:
        raise QueryError(f"recipe {name!r} needs: " + ", ".join(f"--{p}" for p in missing))
    bound = {p: params.get(p) for p in (*required, *optional)}
    try:
        if name == "path-to-state":
            return _path_to_state(store, bound["state"])
        if name == "gaps":
            return _gap_rows(store)
        return _execute(store, RECIPES[name], bound)
    except sqlite3.Error as e:
        raise QueryError(str(e)) from e


def format_table(rows: list[dict], max_cell: int = 60) -> str:
    """Aligned plain-text table; long cells are truncated (table mode only)."""
    if not rows:
        return "(no rows)"
    cols = list(rows[0])

    def cell(v) -> str:
        s = "" if v is None else str(v).replace("\n", " ")
        return s if len(s) <= max_cell else s[:max_cell - 3] + "..."

    body = [[cell(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()  # noqa: E731
    return "\n".join([line(cols), line(["-" * w for w in widths]), *(line(b) for b in body)])
