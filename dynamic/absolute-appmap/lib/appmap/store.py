"""SQLite store for the application map. One DAO method per table.

Naming: ``upsert_<singular>`` is idempotent on the table's UNIQUE key and returns the
row id (a string for states/approvals); ``add_<singular>`` always appends and returns the
new integer id. JSON columns take Python objects and are serialized with json.dumps.
On upsert, only arguments that are not None overwrite existing column values.

Upserts are last-write-wins for JSON list/dict columns (types, examples, statuses, ...):
callers must merge with existing values before calling. ``upsert_state`` increments
``visits`` on every call and never updates ``first_seen_event``.
"""
from __future__ import annotations

import json
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

from appmap import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION", "Store"]


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _b(value: bool | None) -> int | None:
    return None if value is None else int(value)


class Store:
    def __init__(self, db_path: Path, *, _readonly: bool = False):
        self.db_path = Path(db_path)
        if _readonly:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"  # as_uri percent-quotes #, ?, %, spaces
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.conn = sqlite3.connect(self.db_path, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    @classmethod
    def open_readonly(cls, db_path: Path) -> "Store":
        return cls(db_path, _readonly=True)

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        sql = resources.files("appmap").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self._add_missing_columns()
        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif existing.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise RuntimeError(
                f"{self.db_path}: database schema version {existing} is incompatible "
                f"with supported version {SCHEMA_VERSION} (major version differs)")

    # Additive columns introduced after a database may have been created (schema minor
    # changes): table -> [(column, declaration)].
    _ADDED_COLUMNS = {"approvals": [("created_by_run", "INTEGER NOT NULL DEFAULT 0")]}

    def _add_missing_columns(self) -> None:
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # -- helpers -----------------------------------------------------------
    def _insert(self, table: str, data: dict[str, Any]) -> int:
        cols = list(data)
        cur = self.conn.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [data[c] for c in cols],
        )
        return int(cur.lastrowid)

    def _upsert(self, table: str, keys: dict[str, Any], values: dict[str, Any]) -> int:
        """Insert or update on the UNIQUE key; returns the row id.

        Last-write-wins per provided column (JSON columns are replaced, not merged).
        """
        values = {k: v for k, v in values.items() if v is not None}
        cols = list(keys) + list(values)
        sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
               f"ON CONFLICT({','.join(keys)}) ")
        if values:
            sql += "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in values)
        else:
            sql += "DO NOTHING"
        self.conn.execute(sql, [*keys.values(), *values.values()])
        where = " AND ".join(f"{k}=?" for k in keys)
        return int(self.conn.execute(f"SELECT id FROM {table} WHERE {where}", list(keys.values())).fetchone()[0])

    # -- meta / roles ------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    def upsert_role(self, name: str, login_type: str | None = None, auth_ok: bool | None = None,
                    last_login_event: str | None = None) -> str:
        vals = {"login_type": login_type, "auth_ok": _b(auth_ok), "last_login_event": last_login_event}
        vals = {k: v for k, v in vals.items() if v is not None}
        cols = ["name", *vals]
        sql = f"INSERT INTO roles({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT(name) "
        sql += ("DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in vals)) if vals else "DO NOTHING"
        self.conn.execute(sql, [name, *vals.values()])
        return name

    # -- states / transitions / locators ----------------------------------
    def upsert_state(self, id: str, role: str, url: str, route_pattern: str | None = None,
                     dom_fingerprint: str | None = None, title: str | None = None,
                     first_seen_event: str | None = None, screenshot: str | None = None,
                     dom_snapshot: str | None = None) -> str:
        """Insert a state, or bump ``visits`` and refresh provided fields if it exists.

        ``visits`` is incremented on every call; ``first_seen_event`` is set only on insert
        and never updated afterwards.
        """
        vals = {"role": role, "url": url, "route_pattern": route_pattern,
                "dom_fingerprint": dom_fingerprint, "title": title,
                "first_seen_event": first_seen_event, "screenshot": screenshot,
                "dom_snapshot": dom_snapshot}
        vals = {k: v for k, v in vals.items() if v is not None}
        cols = ["id", *vals]
        sets = ",".join(f"{c}=excluded.{c}" for c in vals if c != "first_seen_event")
        sql = (f"INSERT INTO states({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
               f"ON CONFLICT(id) DO UPDATE SET visits=visits+1" + (f",{sets}" if sets else ""))
        self.conn.execute(sql, [id, *vals.values()])
        return id

    def add_transition(self, from_state: str | None, to_state: str | None, role: str,
                       action_type: str, action_label: str | None = None,
                       locator_set_id: int | None = None, event_id: str | None = None,
                       exchange_ids: list[int] | None = None) -> int:
        return self._insert("transitions", dict(
            from_state=from_state, to_state=to_state, role=role, action_type=action_type,
            action_label=action_label, locator_set_id=locator_set_id, event_id=event_id,
            exchange_ids=_j(exchange_ids)))

    def add_locator_set(self, primary_strategy: str | None = None) -> int:
        return self._insert("locator_sets", dict(primary_strategy=primary_strategy))

    def add_locator(self, locator_set_id: int, strategy: str, value: str, rank: int = 0) -> int:
        return self._insert("locators", dict(
            locator_set_id=locator_set_id, strategy=strategy, value=value, rank=rank))

    # -- routes / endpoints / params --------------------------------------
    def upsert_route(self, pattern: str, host: str, kind: str = "page") -> int:
        return self._upsert("routes", dict(host=host, pattern=pattern, kind=kind), {})

    def upsert_endpoint(self, method: str, host: str, path_pattern: str, kind: str,
                        graphql_op: str = "", graphql_type: str | None = None,
                        content_types: list[str] | None = None) -> int:
        return self._upsert(
            "endpoints",
            dict(method=method, host=host, path_pattern=path_pattern, graphql_op=graphql_op),
            dict(kind=kind, graphql_type=graphql_type, content_types=_j(content_types)))

    def upsert_param(self, endpoint_id: int, location: str, name: str,
                     types: list[str] | None = None, examples: list[Any] | None = None,
                     required_evidence: str | None = None) -> int:
        return self._upsert(
            "params", dict(endpoint_id=endpoint_id, location=location, name=name),
            dict(types=_j(types), examples=_j(examples), required_evidence=required_evidence))

    # -- exchanges / responses --------------------------------------------
    def add_exchange(self, endpoint_id: int | None, role: str | None, origin: str,
                     event_id: str | None = None, method: str | None = None, url: str | None = None,
                     req_headers: dict | None = None, req_body: str | None = None,
                     req_body_path: str | None = None, status: int | None = None,
                     resp_headers: dict | None = None, resp_body: str | None = None,
                     resp_body_path: str | None = None, content_type: str | None = None,
                     duration_ms: float | None = None, ts: str | None = None) -> int:
        return self._insert("exchanges", dict(
            endpoint_id=endpoint_id, role=role, origin=origin, event_id=event_id, method=method,
            url=url, req_headers=_j(req_headers), req_body=req_body, req_body_path=req_body_path,
            status=status, resp_headers=_j(resp_headers), resp_body=resp_body,
            resp_body_path=resp_body_path, content_type=content_type, duration_ms=duration_ms,
            ts=ts))

    def set_exchange_body_paths(self, exchange_id: int, req_body_path: str | None = None,
                                resp_body_path: str | None = None) -> None:
        """Record on-disk body files once the exchange id (their filename) is known."""
        vals = {"req_body_path": req_body_path, "resp_body_path": resp_body_path}
        vals = {k: v for k, v in vals.items() if v is not None}
        if vals:
            self.conn.execute(
                f"UPDATE exchanges SET {','.join(f'{c}=?' for c in vals)} WHERE id=?",
                [*vals.values(), exchange_id])

    def upsert_response(self, endpoint_id: int, role: str, statuses: list[int] | None = None,
                        content_types: list[str] | None = None, body_shape: Any = None,
                        min_size: int | None = None, max_size: int | None = None,
                        sample_exchange_ids: list[int] | None = None) -> int:
        return self._upsert(
            "responses", dict(endpoint_id=endpoint_id, role=role),
            dict(statuses=_j(statuses), content_types=_j(content_types), body_shape=_j(body_shape),
                 min_size=min_size, max_size=max_size, sample_exchange_ids=_j(sample_exchange_ids)))

    # -- forms / fields ----------------------------------------------------
    def add_form(self, state_id: str | None, inferred: bool = False, method: str | None = None,
                 action_endpoint_id: int | None = None, purpose: str | None = None,
                 submit_locator_set_id: int | None = None, succeeded: bool | None = None) -> int:
        return self._insert("forms", dict(
            state_id=state_id, inferred=int(inferred), method=method,
            action_endpoint_id=action_endpoint_id, purpose=purpose,
            submit_locator_set_id=submit_locator_set_id, succeeded=_b(succeeded)))

    def add_field(self, form_id: int, name: str, type: str | None = None, widget: str | None = None,
                  constraints: dict | None = None, options: list | None = None,
                  locator_set_id: int | None = None, values_tried: list | None = None,
                  validation_messages: list | None = None) -> int:
        return self._insert("fields", dict(
            form_id=form_id, name=name, type=type, widget=widget, constraints=_j(constraints),
            options=_j(options), locator_set_id=locator_set_id, values_tried=_j(values_tried),
            validation_messages=_j(validation_messages)))

    # -- flows / value links ----------------------------------------------
    def add_flow(self, name: str, role: str | None = None, description: str | None = None,
                 preconditions: list | None = None) -> int:
        return self._insert("flows", dict(
            name=name, role=role, description=description, preconditions=_j(preconditions)))

    def add_flow_step(self, flow_id: int, ordinal: int, transition_id: int | None = None,
                      exchange_id: int | None = None) -> int:
        return self._insert("flow_steps", dict(
            flow_id=flow_id, ordinal=ordinal, transition_id=transition_id, exchange_id=exchange_id))

    def add_value_link(self, producer_exchange_id: int, producer_json_path: str,
                       consumer_exchange_id: int, consumer_location: str, value_hash: str) -> int:
        return self._insert("value_links", dict(
            producer_exchange_id=producer_exchange_id, producer_json_path=producer_json_path,
            consumer_exchange_id=consumer_exchange_id, consumer_location=consumer_location,
            value_hash=value_hash))

    # -- access / provenance ----------------------------------------------
    def upsert_role_access(self, item_kind: str, item_id: int, role: str, outcome: str,
                           evidence_exchange_id: int | None = None) -> int:
        return self._upsert(
            "role_access", dict(item_kind=item_kind, item_id=item_id, role=role),
            dict(outcome=outcome, evidence_exchange_id=evidence_exchange_id))

    def upsert_source(self, item_kind: str, item_id: int, source: str) -> int:
        return self._upsert("sources", dict(item_kind=item_kind, item_id=item_id, source=source), {})

    # -- approvals / observations / links ---------------------------------
    def upsert_approval(self, id: str, role: str | None = None, state_id: str | None = None,
                        category: str | None = None, label: str | None = None,
                        reasons: list | None = None, action_path: list | None = None,
                        predicted_request: dict | None = None, decision: str | None = None,
                        decided_by: str | None = None, executed_event: str | None = None,
                        created_by_run: bool | None = None) -> str:
        vals = dict(role=role, state_id=state_id, category=category, label=label,
                    reasons=_j(reasons), action_path=_j(action_path),
                    predicted_request=_j(predicted_request), decision=decision,
                    decided_by=decided_by, executed_event=executed_event,
                    created_by_run=None if created_by_run is None else int(created_by_run))
        vals = {k: v for k, v in vals.items() if v is not None}
        cols = ["id", *vals]
        sql = f"INSERT INTO approvals({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT(id) "
        sql += ("DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in vals)) if vals else "DO NOTHING"
        self.conn.execute(sql, [id, *vals.values()])
        return id

    def add_observation(self, kind: str, detail: str | None = None, url: str | None = None,
                        role: str | None = None, event_id: str | None = None) -> int:
        return self._insert("observations", dict(
            kind=kind, detail=detail, url=url, role=role, event_id=event_id))

    def upsert_external_link(self, url: str, found_on: str = "", role: str | None = None) -> int:
        return self._upsert("external_links", dict(url=url, found_on=found_on), dict(role=role))
