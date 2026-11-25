"""规则管理（DSL）、名单维护、仿真测试。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.schemas import NameListIn, RuleIn, RuleUpdate, SimulateIn, ValidateIn
from app.core.security import CurrentUser, current_user, require_roles
from app.db.session import DbSession
from app.engine import audit
from app.engine.dsl import DSLError, counterfactual, evaluate, validate
from app.engine.features import FEATURES
from app.models import NameList, Rule
from app.services import simulation, rag
from app.services.runtime import get_runtime

router = APIRouter(tags=["规则与名单"])
strategist = require_roles("strategist")


def _check_expr(expr: str):
    try:
        return validate(expr, set(FEATURES))
    except DSLError as e:
        raise HTTPException(422, f"规则表达式错误：{e}") from None


@router.get("/features", summary="可用特征字段（写规则用）")
def features(_: CurrentUser = Depends(current_user)):
    return [{"name": k, "description": v} for k, v in FEATURES.items()]


@router.post("/rules/validate", summary="校验规则表达式，可带样例特征试算")
def validate_rule(body: ValidateIn, _: CurrentUser = Depends(current_user)):
    ast = _check_expr(body.expression)
    out = {"ok": True}
    if body.sample is not None:
        trace: list[dict] = []
        out["matched"] = evaluate(ast, body.sample, trace)
        out["conditions"] = trace
        out["counterfactual"] = counterfactual(ast, body.sample)
    return out


@router.get("/rules", summary="规则列表")
def list_rules(session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    return session.exec(select(Rule).order_by(Rule.priority, Rule.code)).all()


@router.post("/rules", summary="新建规则", status_code=201)
def create_rule(body: RuleIn, session: Session = DbSession, user: CurrentUser = Depends(strategist)):
    _check_expr(body.expression)
    if session.exec(select(Rule).where(Rule.code == body.code)).first():
        raise HTTPException(409, f"规则编号 {body.code} 已存在")
    rule = Rule(**body.model_dump(), updated_by=user.username)
    session.add(rule)
    session.flush()
    audit.append(session, user.username, "rule.create", "rule", rule.code, body.model_dump())
    rag.ensure_knowledge(session)
    session.commit()
    get_runtime().reload_rules(session)
    return rule


@router.put("/rules/{code}", summary="修改规则（版本号 +1）")
def update_rule(code: str, body: RuleUpdate, session: Session = DbSession,
                user: CurrentUser = Depends(strategist)):
    rule = session.exec(select(Rule).where(Rule.code == code)).first()
    if not rule:
        raise HTTPException(404, "规则不存在")
    changes = body.model_dump(exclude_unset=True)
    if "expression" in changes:
        _check_expr(changes["expression"])
    before = {k: getattr(rule, k) for k in changes}
    for k, v in changes.items():
        setattr(rule, k, v)
    rule.version += 1
    rule.updated_by = user.username
    rule.updated_at = datetime.now()
    session.add(rule)
    session.flush()
    audit.append(session, user.username, "rule.update", "rule", code,
                 {"before": before, "after": changes, "version": rule.version})
    rag.ensure_knowledge(session)
    session.commit()
    get_runtime().reload_rules(session)
    return rule


@router.delete("/rules/{code}", summary="删除规则")
def delete_rule(code: str, session: Session = DbSession, user: CurrentUser = Depends(strategist)):
    rule = session.exec(select(Rule).where(Rule.code == code)).first()
    if not rule:
        raise HTTPException(404, "规则不存在")
    snapshot = rule.model_dump(exclude={"created_at", "updated_at"})
    session.delete(rule)
    session.flush()
    audit.append(session, user.username, "rule.delete", "rule", code, snapshot)
    rag.ensure_knowledge(session)
    session.commit()
    get_runtime().reload_rules(session)
    return {"ok": True}


@router.post("/simulate", summary="仿真测试：候选规则回放历史事件")
def simulate(body: SimulateIn, session: Session = DbSession, _: CurrentUser = Depends(strategist)):
    try:
        return simulation.run(session, get_runtime(), body)
    except DSLError as e:
        raise HTTPException(422, f"规则表达式错误：{e}") from None


# ------------------------------------------------------------------ 名单
@router.get("/namelists", summary="名单列表")
def list_names(list_type: Optional[str] = None, entity_type: Optional[str] = None,
               session: Session = DbSession, _: CurrentUser = Depends(current_user)):
    stmt = select(NameList)
    if list_type:
        stmt = stmt.where(NameList.list_type == list_type)
    if entity_type:
        stmt = stmt.where(NameList.entity_type == entity_type)
    return session.exec(stmt.order_by(NameList.id.desc())).all()


@router.post("/namelists", summary="加入名单", status_code=201)
def add_name(body: NameListIn, session: Session = DbSession,
             user: CurrentUser = Depends(require_roles("strategist", "auditor"))):
    if session.exec(select(NameList).where(NameList.list_type == body.list_type,
                                           NameList.entity_type == body.entity_type,
                                           NameList.value == body.value)).first():
        raise HTTPException(409, "该条目已在名单中")
    row = NameList(**body.model_dump(), created_by=user.username)
    session.add(row)
    session.flush()
    audit.append(session, user.username, "namelist.add", "namelist", row.id, body.model_dump())
    get_runtime().reload_lists(session)
    return row


@router.delete("/namelists/{item_id}", summary="移出名单")
def remove_name(item_id: int, session: Session = DbSession,
                user: CurrentUser = Depends(require_roles("strategist", "auditor"))):
    row = session.get(NameList, item_id)
    if not row:
        raise HTTPException(404, "名单条目不存在")
    snapshot = row.model_dump(exclude={"created_at"})
    session.delete(row)
    session.flush()
    audit.append(session, user.username, "namelist.remove", "namelist", item_id, snapshot)
    get_runtime().reload_lists(session)
    return {"ok": True}
