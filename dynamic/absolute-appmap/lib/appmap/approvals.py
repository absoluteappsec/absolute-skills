"""Approval queue: persisted in the store, reviewed by the user through approvals.yaml."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .store import Store

_DECISIONS = {"approve", "deny", "skip"}
_MATCH_KEYS = {"category", "role", "url_contains", "label_contains"}
_FAILED_KEY = "approval_failed:"  # meta key prefix: why an approved item could not run


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


def _key(role, action_path, label, predicted_request) -> str:
    pr = predicted_request or {}
    return json.dumps([role, action_path, label or "", (pr.get("method") or "").upper(),
                       pr.get("url") or ""], sort_keys=True)


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
        self.warnings: list[str] = []  # populated by load_decisions

    def _rows(self):
        return self.store.conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()

    def _to_pa(self, r) -> PendingAction:
        return PendingAction(id=r["id"], role=r["role"], state_id=r["state_id"], category=r["category"],
                             label=r["label"] or "", reasons=_loads(r["reasons"]) or [],
                             action_path=_loads(r["action_path"]) or [],
                             predicted_request=_loads(r["predicted_request"]))

    def enqueue(self, pa: PendingAction) -> str:
        want = _key(pa.role, pa.action_path, pa.label, pa.predicted_request)
        n = 0
        for r in self._rows():
            if _key(r["role"], _loads(r["action_path"]) or [], r["label"],
                    _loads(r["predicted_request"])) == want:
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
        failed = self.failures()
        items = []
        for r in self._rows():
            item = {"id": r["id"], "role": r["role"], "category": r["category"],
                    "label": r["label"], "reasons": _loads(r["reasons"]) or [],
                    "predicted_request": _loads(r["predicted_request"]),
                    "decision": r["decision"], "note": notes.get(r["id"]) or ""}
            execution = self._execution(r["executed_event"], failed.get(r["id"]))
            if execution:
                item["execution"] = execution  # informational; ignored when loading
            items.append(item)
        out = {"items": items, "rules": old.get("rules") or []}
        self.yaml_path.parent.mkdir(parents=True, exist_ok=True)
        self.yaml_path.write_text(yaml.safe_dump(out, sort_keys=False, default_flow_style=False))

    @staticmethod
    def _rule_error(rule) -> str | None:
        if not isinstance(rule, dict):
            return "rule is not a mapping"
        if rule.get("decision") not in _DECISIONS:
            return f"unrecognised rule decision {rule.get('decision')!r}"
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            return "rule has no match keys"
        for k, v in match.items():
            if k not in _MATCH_KEYS:
                return f"unknown match key {k!r}"
            if not isinstance(v, str) or not v.strip():
                return f"match key {k!r} must be a non-empty string"
        return None

    @staticmethod
    def _rule_matches(match: dict, pa: PendingAction) -> bool:
        if "category" in match and match["category"] != pa.category:
            return False
        if "role" in match and match["role"] != pa.role:
            return False
        if "url_contains" in match and match["url_contains"].lower() not in _request_url(
                pa.predicted_request, pa.action_path).lower():
            return False
        if "label_contains" in match and str(match["label_contains"]).lower() not in pa.label.lower():
            return False
        return True

    def load_decisions(self) -> int:
        """Apply item decisions and rules from approvals.yaml; problems go to self.warnings."""
        self.warnings = []
        data = self._read_yaml()
        changed = 0
        decided_in_items: set[str] = set()
        current = {r["id"]: r for r in self._rows()}
        for item in data.get("items") or []:
            if not isinstance(item, dict) or item.get("id") not in current:
                continue
            id_, d = item["id"], item.get("decision")
            if d in _DECISIONS:
                decided_in_items.add(id_)
                if current[id_]["decision"] != d:
                    self.store.upsert_approval(id_, decision=d, decided_by="user")
                    changed += 1
            elif d == "pending":
                if current[id_]["decision"] != "pending":
                    decided_in_items.add(id_)  # revoked now: rules must not re-decide it this load
                    self.store.conn.execute(
                        "UPDATE approvals SET decision='pending', decided_by=NULL WHERE id=?", (id_,))
                    changed += 1
            elif d not in (None, ""):
                self.warnings.append(f"item {id_}: unrecognised decision {d!r} ignored")
        rules = []
        for n, rule in enumerate(data.get("rules") or [], 1):
            err = self._rule_error(rule)
            if err:
                self.warnings.append(f"rule #{n} skipped: {err}")
            else:
                rules.append(rule)
        for id_, r in current.items():
            if id_ in decided_in_items or r["decision"] != "pending":
                continue
            pa = self._to_pa(r)
            for rule in rules:
                if self._rule_matches(rule["match"], pa):
                    self.store.upsert_approval(id_, decision=rule["decision"], decided_by="rule")
                    changed += 1
                    break
        return changed

    def approved_unexecuted(self) -> list[PendingAction]:
        return [self._to_pa(r) for r in self._rows()
                if r["decision"] == "approve" and r["executed_event"] is None]

    def mark_executed(self, id: str, event_id: str) -> None:
        self.store.upsert_approval(id, executed_event=event_id)
        self.store.conn.execute("DELETE FROM meta WHERE key=?", (_FAILED_KEY + id,))

    def mark_failed(self, id: str, reason: str) -> None:
        """Record why an approved item could not be (fully) executed."""
        self.store.set_meta(_FAILED_KEY + id, reason)

    def failures(self) -> dict[str, str]:
        return {r["key"][len(_FAILED_KEY):]: r["value"] for r in self.store.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ?", (_FAILED_KEY + "%",))}

    @staticmethod
    def _execution(event_id: str | None, failure: str | None) -> str:
        if event_id and failure:
            return f"executed ({event_id}) with error: {failure}"
        if event_id:
            return f"executed ({event_id})"
        return f"failed: {failure}" if failure else ""

    def summary(self) -> dict[str, int]:
        """Status counts for CLI output (decision names spelled out, plus failures)."""
        c = self.counts()
        return {"pending": c["pending"], "approved": c["approve"], "denied": c["deny"],
                "skipped": c["skip"], "executed": c["executed"],
                "approved_unexecuted": len(self.approved_unexecuted()),
                "failed": len(self.failures())}

    def counts(self) -> dict[str, int]:
        c = {"pending": 0, "approve": 0, "deny": 0, "skip": 0, "executed": 0}
        for r in self._rows():
            c[r["decision"]] += 1
            if r["executed_event"] is not None:
                c["executed"] += 1
        return c
