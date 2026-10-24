"""Approval queue: persisted in the store, reviewed by the user through approvals.yaml."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .store import Store

_DECISIONS = {"approve", "deny", "skip"}


@dataclass
class PendingAction:
    id: str
    role: str
    state_id: str | None
    category: str | None
    label: str
    reasons: list[str] = field(default_factory=list)
    action_path: list[dict] = field(default_factory=list)
    predicted_request: dict | None = None


def _loads(v):
    return json.loads(v) if v else None


def _key(role, action_path) -> str:
    return json.dumps([role, action_path], sort_keys=True)


def _request_url(predicted_request, action_path) -> str:
    if predicted_request and predicted_request.get("url"):
        return predicted_request["url"]
    for step in reversed(action_path or []):
        for k in ("goto", "url"):
            if step.get(k):
                return str(step[k])
    return ""


class ApprovalQueue:
    def __init__(self, store: Store, yaml_path: Path):
        self.store = store
        self.yaml_path = Path(yaml_path)

    def _rows(self):
        return self.store.conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()

    def _to_pa(self, r) -> PendingAction:
        return PendingAction(id=r["id"], role=r["role"], state_id=r["state_id"], category=r["category"],
                             label=r["label"] or "", reasons=_loads(r["reasons"]) or [],
                             action_path=_loads(r["action_path"]) or [],
                             predicted_request=_loads(r["predicted_request"]))

    def enqueue(self, pa: PendingAction) -> str:
        want = _key(pa.role, pa.action_path)
        n = 0
        for r in self._rows():
            if _key(r["role"], _loads(r["action_path"]) or []) == want:
                return r["id"]
            try:
                n = max(n, int(r["id"].split("-")[1]))
            except (IndexError, ValueError):
                pass
        id_ = f"ap-{n + 1:04d}"
        self.store.upsert_approval(id_, role=pa.role, state_id=pa.state_id, category=pa.category,
                                   label=pa.label, reasons=pa.reasons, action_path=pa.action_path,
                                   predicted_request=pa.predicted_request, decision="pending")
        pa.id = id_
        return id_

    def _read_yaml(self) -> dict:
        if not self.yaml_path.exists():
            return {}
        data = yaml.safe_load(self.yaml_path.read_text()) or {}
        return data if isinstance(data, dict) else {}

    def write_yaml(self) -> None:
        old = self._read_yaml()
        notes = {i.get("id"): i.get("note") for i in old.get("items") or [] if isinstance(i, dict)}
        items = [{"id": r["id"], "role": r["role"], "category": r["category"], "label": r["label"],
                  "reasons": _loads(r["reasons"]) or [],
                  "predicted_request": _loads(r["predicted_request"]),
                  "decision": r["decision"], "note": notes.get(r["id"]) or ""}
                 for r in self._rows()]
        out = {"items": items, "rules": old.get("rules") or []}
        self.yaml_path.parent.mkdir(parents=True, exist_ok=True)
        self.yaml_path.write_text(yaml.safe_dump(out, sort_keys=False, default_flow_style=False))

    @staticmethod
    def _rule_matches(match: dict, pa: PendingAction) -> bool:
        if "category" in match and match["category"] != pa.category:
            return False
        if "role" in match and match["role"] != pa.role:
            return False
        if "url_contains" in match and str(match["url_contains"]) not in _request_url(
                pa.predicted_request, pa.action_path):
            return False
        if "label_contains" in match and str(match["label_contains"]).lower() not in pa.label.lower():
            return False
        return True

    def load_decisions(self) -> int:
        data = self._read_yaml()
        changed = 0
        decided_in_items: set[str] = set()
        current = {r["id"]: r for r in self._rows()}
        for item in data.get("items") or []:
            if not isinstance(item, dict) or item.get("id") not in current:
                continue
            d = item.get("decision")
            if d in _DECISIONS:
                decided_in_items.add(item["id"])
                if current[item["id"]]["decision"] != d:
                    self.store.upsert_approval(item["id"], decision=d, decided_by="user")
                    changed += 1
        rules = [r for r in data.get("rules") or []
                 if isinstance(r, dict) and r.get("decision") in _DECISIONS]
        for id_, r in current.items():
            if id_ in decided_in_items or r["decision"] != "pending":
                continue
            pa = self._to_pa(r)
            for rule in rules:
                if self._rule_matches(rule.get("match") or {}, pa):
                    self.store.upsert_approval(id_, decision=rule["decision"], decided_by="rule")
                    changed += 1
                    break
        return changed

    def approved_unexecuted(self) -> list[PendingAction]:
        return [self._to_pa(r) for r in self._rows()
                if r["decision"] == "approve" and r["executed_event"] is None]

    def mark_executed(self, id: str, event_id: str) -> None:
        self.store.upsert_approval(id, executed_event=event_id)

    def counts(self) -> dict[str, int]:
        c = {"pending": 0, "approve": 0, "deny": 0, "skip": 0, "executed": 0}
        for r in self._rows():
            c[r["decision"]] += 1
            if r["executed_event"] is not None:
                c["executed"] += 1
        return c
