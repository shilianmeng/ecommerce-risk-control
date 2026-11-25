"""事件网关 & 决策回溯。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, func, select

from app.api.schemas import EventIn
from app.core import config
from app.engine.whatif import decision_whatif
from app.core.security import CurrentUser, current_user, event_caller
from app.db.session import DbSession
from app.models import AuditLog, Decision, EventProjection, RiskCase, RiskEvent
from app.engine.features import LateEventError
from app.services import decision as dsvc
from app.services.runtime import get_runtime

router = APIRouter(tags=["事件网关与决策"])
log = logging.getLogger(__name__)


@router.post("/events", summary="接入事件并实时决策")
def ingest(body: EventIn, session: Session = DbSession, caller: str = Depends(event_caller)):
    """业务系统调用（请求头 X-Risk-Api-Key），或控制台用户用登录 Token 调用。
    返回 PASS / REVIEW / REJECT 前提交快照、案件及审计。"""
    ev = body.model_dump()
    log.info("事件接入 caller=%s type=%s order=%s", caller, ev.get("event_type"), ev.get("order_id"))
    try:
        result, duplicate = dsvc.decide(get_runtime(), session, ev)
    except dsvc.DuplicateInFlight:
        raise HTTPException(409, "同一事件正在处理中，请稍后重试") from None
    except LateEventError as e:
        raise HTTPException(422, str(e)) from None
    except SQLAlchemyError:
        log.exception("事件事务提交失败")
        raise HTTPException(503, "决策未确认，请使用同一事件重试") from None
    return {"duplicate": duplicate, **result}


@router.get("/decisions", summary="决策记录列表")
def list_decisions(decision: Optional[str] = None, event_type: Optional[str] = None,
                   user_id: Optional[str] = None, page: int = Query(1, ge=1),
                   size: int = Query(20, ge=1, le=200), session: Session = DbSession,
                   _: CurrentUser = Depends(current_user)):
    stmt = select(Decision)
    cnt = select(func.count()).select_from(Decision)
    for col, val in ((Decision.decision, decision), (Decision.event_type, event_type), (Decision.user_id, user_id)):
        if val:
            stmt, cnt = stmt.where(col == val), cnt.where(col == val)
    rows = session.exec(stmt.order_by(Decision.event_time.desc(), Decision.id.desc())
                        .offset((page - 1) * size).limit(size)).all()
    return {"total": session.exec(cnt).one(),
            "items": [{"id": d.id, "idem_key": d.idem_key, "event_type": d.event_type, "user_id": d.user_id,
                       "decision": d.decision, "score": d.score, "decided_by": d.decided_by,
                       "hit_codes": [h["code"] for h in d.hits], "event_time": d.event_time,
                       "latency_ms": d.latency_ms} for d in rows]}


@router.get("/decisions/{decision_id}", summary="决策回溯：命中规则 + 单项分值 + 特征快照")
def trace(decision_id: int, session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    d = session.get(Decision, decision_id)
    if not d:
        raise HTTPException(404, "决策不存在")
    ev = session.get(RiskEvent, d.event_pk)
    case = session.exec(select(RiskCase).where(RiskCase.decision_id == d.id)).first()
    return {
        "decision": d.model_dump(),
        "event": ev.model_dump() if ev else None,
        "case_no": case.case_no if case else None,
        "explain": _explain(d),
        "whatif": decision_whatif(d.model_dump(), config.REVIEW_THRESHOLD, config.REJECT_THRESHOLD),
        "original": (job.original if (job := session.get(EventProjection, d.idem_key)) else None),
        "corrections": [a.model_dump() for a in session.exec(select(AuditLog).where(
            AuditLog.target_type == "decision", AuditLog.target_id == str(d.id),
            AuditLog.action.in_(["decision.recompute", "decision.recompute_required"]))
            .order_by(AuditLog.id)).all()],
    }


@router.get("/decisions/by-key/{idem_key}", summary="按幂等键查决策")
def trace_by_key(idem_key: str, session: Session = DbSession, user: CurrentUser = Depends(current_user)):
    d = session.exec(select(Decision).where(Decision.idem_key == idem_key)).first()
    if not d:
        raise HTTPException(404, "决策不存在")
    return trace(d.id, session, user)


def _explain(d: Decision) -> str:
    """把回溯数据翻译成一句人话。"""
    if d.decided_by == "whitelist":
        return "命中白名单，直接放行。"
    if d.decided_by == "blacklist":
        hit = "、".join(f"{h['entity_type']}={h['value']}" for h in d.list_hits if h["list_type"] == "black")
        return f"命中黑名单（{hit}），直接拒绝，风险分 100。"
    if not d.hits:
        return f"未命中任何规则，风险分 0，结论 {d.decision}。"
    parts = "；".join(f"{h['code']} {h['name']} +{h['score']}" for h in d.hits)
    return f"命中 {len(d.hits)} 条规则：{parts}；累计 {d.score} 分 → {d.decision}。"
