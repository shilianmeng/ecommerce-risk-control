"""案件审核工作台、关联图谱、审计链。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, func, select

from app.api.schemas import AssistIn, DisposeIn
from app.core import config
from app.core.security import CurrentUser, current_user, require_roles
from app.db.session import DbSession
from app.engine import audit
from app.engine.whatif import decision_whatif
from app.models import AuditLog, Decision, RiskCase, RiskEvent
from app.services import cases as csvc
from app.services import rag
from app.services.runtime import get_runtime

router = APIRouter(tags=["案件与图谱"])
auditor = require_roles("auditor")


@router.get("/cases", summary="案件列表")
def list_cases(status: Optional[str] = None, decision: Optional[str] = None, user_id: Optional[str] = None,
               page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
               session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    stmt = select(RiskCase)
    cnt = select(func.count()).select_from(RiskCase)
    for col, val in ((RiskCase.status, status), (RiskCase.decision, decision), (RiskCase.user_id, user_id)):
        if val:
            stmt, cnt = stmt.where(col == val), cnt.where(col == val)
    rows = session.exec(stmt.order_by(RiskCase.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"total": session.exec(cnt).one(), "items": rows}


@router.get("/cases/{case_no}", summary="案件详情：画像 + 特征 + 决策回溯 + 图谱 + 审计")
def case_detail(case_no: str, session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    case = session.exec(select(RiskCase).where(RiskCase.case_no == case_no)).first()
    if not case:
        raise HTTPException(404, "案件不存在")
    d = session.get(Decision, case.decision_id)
    ev = session.get(RiskEvent, d.event_pk) if d else None
    rt = get_runtime()
    try:
        graph = rt.graph.neighborhood(case.user_id, hops=2)
        related = rt.graph.related_accounts(case.user_id, hops=2)
    except Exception as e:
        graph, related = {"nodes": [], "edges": [], "error": str(e)}, []

    # 账号画像：历史决策分布 + 历史案件
    dist = dict(session.exec(select(Decision.decision, func.count()).where(Decision.user_id == case.user_id)
                             .group_by(Decision.decision)).all())
    history = session.exec(select(RiskCase).where(RiskCase.user_id == case.user_id, RiskCase.id != case.id)
                           .order_by(RiskCase.id.desc()).limit(10)).all()
    logs = session.exec(select(AuditLog).where(AuditLog.target_type == "case", AuditLog.target_id == case_no)
                        .order_by(AuditLog.id)).all()
    return {
        "case": case,
        "decision": d,
        "whatif": decision_whatif(d.model_dump(), config.REVIEW_THRESHOLD, config.REJECT_THRESHOLD) if d else None,
        "event": ev,
        "profile": {"user_id": case.user_id, "decision_distribution": dist,
                    "related_accounts": related, "history_cases": history},
        "graph": graph,
        "audit": logs,
    }


@router.post("/cases/{case_no}/claim", summary="认领（待审 → 审核中）")
def claim(case_no: str, session: Session = DbSession, user: CurrentUser = Depends(auditor)):
    return _wrap(lambda: csvc.claim(session, case_no, user.username))


@router.post("/cases/{case_no}/dispose", summary="处置（审核中 → 已处置）")
def dispose(case_no: str, body: DisposeIn, session: Session = DbSession,
            user: CurrentUser = Depends(auditor)):
    case, added = _wrap(lambda: csvc.dispose(session, case_no, user.username, body.actions,
                                             body.conclusion, body.note))
    if added:
        session.flush()
        get_runtime().reload_lists(session)
    return {"case": case, "namelist_added": added}


@router.post("/cases/{case_no}/archive", summary="归档（已处置 → 已归档）")
def archive(case_no: str, session: Session = DbSession, user: CurrentUser = Depends(auditor)):
    return _wrap(lambda: csvc.archive(session, case_no, user.username))


def _wrap(fn):
    try:
        return fn()
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except csvc.CaseError as e:
        raise HTTPException(409, str(e)) from None


# ------------------------------------------------------------------ RAG 审核辅助
@router.post("/cases/{case_no}/assist", summary="RAG 审核辅助：规则解释 + 相似判例 + 处置建议（带出处）")
def case_assist(case_no: str, body: Optional[AssistIn] = None, session: Session = DbSession,
                _: CurrentUser = Depends(current_user)):
    case = session.exec(select(RiskCase).where(RiskCase.case_no == case_no)).first()
    if not case:
        raise HTTPException(404, "案件不存在")
    return rag.assist(session, case, body.question if body else None)


@router.get("/knowledge/search", summary="知识库检索（政策 / 判例 / FAQ / 规则说明）")
def knowledge_search(q: str = Query(..., min_length=1, max_length=200), k: int = Query(5, ge=1, le=20),
                     session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    return {"query": q, "items": rag.search(session, q, k)}

# ------------------------------------------------------------------ 图谱
@router.get("/graph/accounts/{user_id}", summary="N 跳关联查询")
def neighborhood(user_id: str, hops: int = Query(2, ge=1, le=3), _: CurrentUser = Depends(current_user)):
    rt = get_runtime()
    return {"backend": rt.graph.backend, "related_accounts": rt.graph.related_accounts(user_id, hops),
            **rt.graph.neighborhood(user_id, hops)}


@router.get("/graph/gangs", summary="团伙识别（连通分量）")
def gangs(min_size: int = Query(3, ge=2, le=100), _: CurrentUser = Depends(current_user)):
    rt = get_runtime()
    return {"backend": rt.graph.backend, "gangs": rt.graph.gangs(min_size)}


# ------------------------------------------------------------------ 审计
@router.get("/audit", summary="审计日志")
def audit_logs(target_type: Optional[str] = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=500),
               session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    stmt = select(AuditLog)
    cnt = select(func.count()).select_from(AuditLog)
    if target_type:
        stmt, cnt = stmt.where(AuditLog.target_type == target_type), cnt.where(AuditLog.target_type == target_type)
    rows = session.exec(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"total": session.exec(cnt).one(), "items": rows}


@router.get("/audit/verify", summary="校验审计哈希链")
def audit_verify(session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    return audit.verify(session)
