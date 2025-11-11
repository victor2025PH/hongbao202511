# -*- coding: utf-8 -*-
"""
Simple audit logging service for sensitive operations.

Usage:
    from web_admin.services.audit_service import record_audit
    record_audit(action="export_all", operator=uid, payload={"status": "success"})
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AuditEntry:
    seq: int
    action: str
    operator: int
    timestamp: datetime
    payload: Dict[str, Any] = field(default_factory=dict)


_AUDIT_LOG: List[AuditEntry] = []
_AUDIT_KEYS: set[tuple[str, int, str]] = set()
_AUDIT_SEQ: int = 0


def clear_audit_entries() -> None:
    _AUDIT_LOG.clear()
    _AUDIT_KEYS.clear()
    global _AUDIT_SEQ
    _AUDIT_SEQ = 0


def record_audit(action: str, operator: int, payload: Optional[Dict[str, Any]] = None) -> bool:
    if operator <= 0:
        return False
    payload = payload or {}
    payload_key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    key = (action, operator, payload_key)
    if key in _AUDIT_KEYS:
        return False

    global _AUDIT_SEQ
    entry = AuditEntry(
        seq=_AUDIT_SEQ,
        action=action,
        operator=operator,
        timestamp=datetime.utcnow(),
        payload=payload or {},
    )
    _AUDIT_LOG.append(entry)
    _AUDIT_KEYS.add(key)
    _AUDIT_SEQ += 1
    # placeholder for future notification hook
    # e.g., send_to_slack(entry)
    return True


def list_audit_entries(
    action: Optional[str] = None,
    operator: Optional[int] = None,
    reverse: bool = False,
) -> List[AuditEntry]:
    entries = [
        entry
        for entry in _AUDIT_LOG
        if (action is None or entry.action == action)
        and (operator is None or entry.operator == operator)
    ]
    entries.sort(key=lambda e: (e.timestamp, e.seq), reverse=reverse)
    return entries


def audit_as_json() -> str:
    data = [
        {
            "action": entry.action,
            "operator": entry.operator,
            "timestamp": entry.timestamp.isoformat(),
            "payload": entry.payload,
        }
        for entry in list_audit_entries()
    ]
    return json.dumps(data, ensure_ascii=False, indent=2)

