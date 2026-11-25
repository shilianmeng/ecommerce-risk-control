"""接口入参/出参模型（Pydantic）。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

EventType = Literal["login", "coupon_receive", "order_create", "order_pay", "after_sale_apply"]


class EventIn(BaseModel):
    """事件网关入参。ts 为事件发生时间（Unix 秒，可带小数；传毫秒也会自动识别）。"""
    event_type: EventType
    user_id: str = Field(min_length=1, max_length=64)
    ts: Optional[float] = None
    device_id: Optional[str] = Field(default=None, max_length=64)
    ip: Optional[str] = Field(default=None, max_length=64)
    phone: Optional[str] = Field(default=None, max_length=32)
    address: Optional[str] = Field(default=None, max_length=255)
    order_id: Optional[str] = Field(default=None, max_length=64)
    amount: float = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _ts(cls, v: Optional[float]) -> float:
        if v is None:
            return round(time.time(), 3)
        if v > 1e12:                       # 毫秒
            v = v / 1000
        if v < 1e9 or v > time.time() + 300:
            raise ValueError("ts 不合法（应为 Unix 秒，且不能晚于当前时间 5 分钟以上）")
        return round(v, 3)

    @model_validator(mode="after")
    def _need_order(self):
        if self.event_type in ("order_create", "order_pay", "after_sale_apply") and not self.order_id:
            raise ValueError(f"{self.event_type} 事件必须带 order_id")
        if self.event_type == "after_sale_apply" and "refund_reason" not in self.payload:
            raise ValueError("after_sale_apply 事件的 payload 必须带 refund_reason")
        return self


class LoginIn(BaseModel):
    username: str
    password: str


class RuleIn(BaseModel):
    code: str = Field(pattern=r"^[A-Z]-?\d{3}$|^[A-Z]{1,4}-\d{1,4}$", max_length=32)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    event_types: list[EventType] = Field(default_factory=list)
    expression: str = Field(min_length=1, max_length=2000)
    score: int = Field(ge=-100, le=100)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10000)


class RuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    event_types: Optional[list[EventType]] = None
    expression: Optional[str] = Field(default=None, max_length=2000)
    score: Optional[int] = Field(default=None, ge=-100, le=100)
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=10000)


class ValidateIn(BaseModel):
    expression: str
    sample: Optional[dict[str, Any]] = None


class NameListIn(BaseModel):
    list_type: Literal["white", "black", "grey"]
    entity_type: Literal["user", "device", "ip", "phone", "address"]
    value: str = Field(min_length=1, max_length=191)
    reason: str = Field(default="", max_length=255)
    expire_at: Optional[datetime] = None


class DisposeIn(BaseModel):
    actions: list[Literal["block_order", "blacklist_user", "ban_device", "release"]]
    conclusion: Literal["fraud", "normal"]
    note: str = Field(default="", max_length=2000)


class AssistIn(BaseModel):
    question: Optional[str] = Field(default=None, max_length=500)


class SimRule(BaseModel):
    code: str
    name: str = ""
    event_types: list[EventType] = Field(default_factory=list)
    expression: str
    score: int = Field(ge=-100, le=100)


class SimulateIn(BaseModel):
    """仿真：拿候选规则集回放历史事件。rules 为空 = 用当前线上规则；
    draft 可在线上规则基础上追加/覆盖单条规则（按 code）。"""
    rules: Optional[list[SimRule]] = None
    draft: Optional[SimRule] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    event_types: list[EventType] = Field(default_factory=list)
    limit: int = Field(default=2000, ge=1, le=20000)
