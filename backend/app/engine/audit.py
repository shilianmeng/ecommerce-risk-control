"""审计哈希链。

每条日志：hash = SHA256(prev_hash | actor | action | target_type | target_id | detail(JSON 规范化) | created_at)
第一条的 prev_hash 是 64 个 0。任何一条被改、被删、被插入，从那条往后都会对不上。

并发写入时要保证 prev_hash 取到的是真正的上一条：写入在进程内加锁，并在 MySQL 上
用 SELECT … FOR UPDATE 锁住链尾（多进程部署时同样有效）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.models import AuditLog

GENESIS = "0" * 64
_lock = threading.Lock()


def _canonical(detail: dict[str, Any]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, actor: str, action: str, target_type: str, target_id: str,
                 detail: dict[str, Any], created_at: datetime) -> str:
    raw = "|".join([prev_hash, actor, action, target_type, str(target_id), _canonical(detail),
                    created_at.strftime("%Y-%m-%d %H:%M:%S")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def append(session: Session, actor: str, action: str, target_type: str, target_id: Any,
           detail: dict[str, Any] | None = None) -> AuditLog:
    detail = json.loads(_canonical(detail or {}))     # 与校验时序列化口径一致
    with _lock:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        if session.get_bind().dialect.name == "mysql":
            stmt = stmt.with_for_update()
        last = session.exec(stmt).first()
        prev = last.hash if last else GENESIS
        now = datetime.now().replace(microsecond=0)   # MySQL DATETIME 只存到秒
        entry = AuditLog(actor=actor, action=action, target_type=target_type, target_id=str(target_id),
                         detail=detail, created_at=now, prev_hash=prev,
                         hash=compute_hash(prev, actor, action, target_type, str(target_id), detail, now))
        session.add(entry)
        session.flush()
    return entry


def verify(session: Session) -> dict[str, Any]:
    """从头校验整条链，返回第一个断点。"""
    prev = GENESIS
    count = 0
    for e in session.exec(select(AuditLog).order_by(AuditLog.id)):
        count += 1
        if e.prev_hash != prev:
            return {"ok": False, "checked": count, "broken_at": e.id,
                    "reason": "prev_hash 与上一条不符（有记录被删除或插入）"}
        expect = compute_hash(e.prev_hash, e.actor, e.action, e.target_type, e.target_id,
                              e.detail or {}, e.created_at)
        if expect != e.hash:
            return {"ok": False, "checked": count, "broken_at": e.id,
                    "reason": "内容与 hash 不符（记录被篡改）"}
        prev = e.hash
    return {"ok": True, "checked": count, "broken_at": None, "reason": ""}
