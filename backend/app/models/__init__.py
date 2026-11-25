"""数据表定义（SQLModel）。所有业务表都在这里注册。

时间一律按本地时间（naive）存储：SQLModel 0.0.50 默认的 UTCDateTime 要求带时区，这里显式用 DateTime。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


class SysUser(SQLModel, table=True):
    """系统账号：admin 管理员 / strategist 策略师 / auditor 审核员。"""
    __tablename__ = "sys_user"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(max_length=64, unique=True, index=True)
    display_name: str = Field(max_length=64)
    role: str = Field(max_length=16)
    password_hash: str = Field(max_length=128)
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class RiskEvent(SQLModel, table=True):
    """事件网关接收的原始事件（五类）。idem_key 唯一 = 持久化层的幂等兜底。"""
    __tablename__ = "risk_event"
    id: Optional[int] = Field(default=None, primary_key=True)
    idem_key: str = Field(max_length=191, unique=True, index=True)
    event_type: str = Field(max_length=32, index=True)
    user_id: str = Field(max_length=64, index=True)
    device_id: Optional[str] = Field(default=None, max_length=64, index=True)
    ip: Optional[str] = Field(default=None, max_length=64)
    phone: Optional[str] = Field(default=None, max_length=32)
    address: Optional[str] = Field(default=None, max_length=255)
    order_id: Optional[str] = Field(default=None, max_length=64, index=True)
    amount: float = 0.0
    event_time: datetime = Field(sa_type=DateTime, index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class Decision(SQLModel, table=True):
    """决策记录：命中规则 + 单项分值 + 特征快照，全链路可回溯。"""
    __tablename__ = "decision"
    id: Optional[int] = Field(default=None, primary_key=True)
    event_pk: int = Field(index=True, foreign_key="risk_event.id")
    idem_key: str = Field(max_length=191, unique=True, index=True)
    event_type: str = Field(max_length=32, index=True)
    user_id: str = Field(max_length=64, index=True)
    decision: str = Field(max_length=8, index=True)           # PASS / REVIEW / REJECT
    score: int = 0
    decided_by: str = Field(max_length=16, default="rules")   # whitelist / blacklist / rules
    hits: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    list_hits: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    features: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    rule_version: str = Field(default="", max_length=64)
    latency_ms: float = 0.0
    event_time: datetime = Field(sa_type=DateTime, index=True)
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now, index=True)


class Rule(SQLModel, table=True):
    """规则：DSL 表达式 + 分值，按事件类型生效。"""
    __tablename__ = "rule"
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(max_length=32, unique=True, index=True)
    name: str = Field(max_length=128)
    description: str = Field(default="", sa_column=Column(Text))
    event_types: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    expression: str = Field(sa_column=Column(Text, nullable=False))
    score: int = 10
    enabled: bool = True
    priority: int = 100
    version: int = 1
    updated_by: str = Field(default="system", max_length=64)
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)
    updated_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class NameList(SQLModel, table=True):
    """名单库：white / black / grey × user / device / ip / phone / address。"""
    __tablename__ = "name_list"
    __table_args__ = (UniqueConstraint("list_type", "entity_type", "value", name="uq_namelist"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    list_type: str = Field(max_length=8, index=True)
    entity_type: str = Field(max_length=16, index=True)
    value: str = Field(max_length=191, index=True)
    reason: str = Field(default="", max_length=255)
    expire_at: Optional[datetime] = Field(default=None, sa_type=DateTime)
    created_by: str = Field(default="system", max_length=64)
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class RiskCase(SQLModel, table=True):
    """案件：待审 → 审核中 → 已处置 → 已归档。"""
    __tablename__ = "risk_case"
    id: Optional[int] = Field(default=None, primary_key=True)
    case_no: str = Field(max_length=32, unique=True, index=True)
    decision_id: int = Field(index=True, foreign_key="decision.id")
    event_type: str = Field(max_length=32)
    user_id: str = Field(max_length=64, index=True)
    decision: str = Field(max_length=8)
    score: int = 0
    status: str = Field(max_length=16, index=True, default="PENDING")
    assignee: Optional[str] = Field(default=None, max_length=64)
    actions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    conclusion: Optional[str] = Field(default=None, max_length=16)   # fraud / normal
    note: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now, index=True)
    updated_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class AuditLog(SQLModel, table=True):
    """审计日志（哈希链）：hash = sha256(prev_hash + 规范化内容)，改任何一条都会断链。"""
    __tablename__ = "audit_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    actor: str = Field(max_length=64)
    action: str = Field(max_length=64, index=True)
    target_type: str = Field(max_length=32)
    target_id: str = Field(max_length=64, index=True)
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)
    prev_hash: str = Field(max_length=64)
    hash: str = Field(max_length=64, unique=True)


class KnowledgeDoc(SQLModel, table=True):
    """RAG 知识库片段：规则手册 / 判例 / 赔付政策 / FAQ。"""
    __tablename__ = "knowledge_doc"
    id: Optional[int] = Field(default=None, primary_key=True)
    doc_code: str = Field(max_length=32, unique=True, index=True)
    category: str = Field(max_length=16, index=True)          # rule / case / policy / faq
    title: str = Field(max_length=128)
    content: str = Field(sa_column=Column(Text, nullable=False))
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(sa_type=DateTime, default_factory=_now)


class DecisionGuard(SQLModel, table=True):
    """事务级串行化；更新同一行，MySQL / SQLite 都持锁到提交。"""
    id: int = Field(default=1, primary_key=True)
    revision: int = 0


class EventProjection(SQLModel, table=True):
    """与决策同事务提交的投影任务，以及可重算的原始策略快照。"""
    idem_key: str = Field(max_length=191, primary_key=True)
    event: dict[str, Any] = Field(sa_column=Column(JSON))
    rules: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    settings: dict[str, Any] = Field(sa_column=Column(JSON))
    original: dict[str, Any] = Field(sa_column=Column(JSON))
    projected: bool = Field(default=False, index=True)
