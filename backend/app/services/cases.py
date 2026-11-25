"""案件状态机与处置。

    PENDING(待审) --claim--> IN_REVIEW(审核中) --dispose--> DISPOSED(已处置) --archive--> ARCHIVED(已归档)

非法跳转一律拒绝；每一步都写审计哈希链。
处置动作：block_order 拦截订单 / blacklist_user 拉黑账号 / ban_device 封禁设备 / release 放行。
拉黑、封禁会真正写进名单库并刷新名单缓存，下一笔事件立即生效。
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.engine import audit
from app.models import Decision, NameList, RiskCase, RiskEvent

STATUS_CN = {"PENDING": "待审", "IN_REVIEW": "审核中", "DISPOSED": "已处置", "ARCHIVED": "已归档"}
TRANSITIONS = {
    "claim": ("PENDING", "IN_REVIEW"),
    "dispose": ("IN_REVIEW", "DISPOSED"),
    "archive": ("DISPOSED", "ARCHIVED"),
}
ACTIONS = {"block_order": "拦截订单", "blacklist_user": "拉黑账号", "ban_device": "封禁设备", "release": "放行"}


class CaseError(ValueError):
    pass


def _get(session: Session, case_no: str) -> RiskCase:
    case = session.exec(select(RiskCase).where(RiskCase.case_no == case_no)).first()
    if not case:
        raise LookupError(f"案件 {case_no} 不存在")
    return case


def _move(case: RiskCase, op: str) -> str:
    src, dst = TRANSITIONS[op]
    if case.status != src:
        raise CaseError(f"案件当前是「{STATUS_CN[case.status]}」，不能执行此操作（需要「{STATUS_CN[src]}」）")
    old = case.status
    case.status = dst
    case.updated_at = datetime.now()
    return old


def claim(session: Session, case_no: str, actor: str) -> RiskCase:
    case = _get(session, case_no)
    old = _move(case, "claim")
    case.assignee = actor
    audit.append(session, actor, "case.claim", "case", case_no, {"from": old, "to": case.status})
    return case


def dispose(session: Session, case_no: str, actor: str, actions: list[str], conclusion: str,
            note: str = "") -> tuple[RiskCase, list[str]]:
    """返回 (案件, 写入名单的条目描述)。调用方负责刷新名单缓存。"""
    unknown = [a for a in actions if a not in ACTIONS]
    if unknown or not actions:
        raise CaseError(f"处置动作无效：{unknown or '至少选一个'}")
    if "release" in actions and len(actions) > 1:
        raise CaseError("放行不能和拦截类动作同时选")
    if conclusion not in ("fraud", "normal"):
        raise CaseError("结论只能是 fraud（确认欺诈）或 normal（误判）")
    case = _get(session, case_no)
    if case.assignee and case.assignee != actor:
        raise CaseError(f"案件已由 {case.assignee} 认领，只有认领人可以处置")
    old = _move(case, "dispose")

    decision = session.get(Decision, case.decision_id)
    event = session.get(RiskEvent, decision.event_pk) if decision else None
    added: list[str] = []
    for act, etype, value in (("blacklist_user", "user", case.user_id),
                              ("ban_device", "device", event.device_id if event else None)):
        if act in actions and value:
            exists = session.exec(select(NameList).where(
                NameList.list_type == "black", NameList.entity_type == etype, NameList.value == value)).first()
            if not exists:
                session.add(NameList(list_type="black", entity_type=etype, value=value,
                                     reason=f"案件 {case_no} 处置", created_by=actor))
                added.append(f"black:{etype}:{value}")

    case.actions = actions
    case.conclusion = conclusion
    case.note = note
    audit.append(session, actor, "case.dispose", "case", case_no,
                 {"from": old, "to": case.status, "actions": actions, "conclusion": conclusion,
                  "note": note, "namelist_added": added,
                  "order_id": event.order_id if event else None})
    return case, added


def archive(session: Session, case_no: str, actor: str) -> RiskCase:
    case = _get(session, case_no)
    old = _move(case, "archive")
    audit.append(session, actor, "case.archive", "case", case_no, {"from": old, "to": case.status})
    return case
