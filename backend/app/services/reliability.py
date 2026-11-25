"""SQL 为决策事实源；外部存储为可重试投影；迟到事件在同一事务重算。"""
from __future__ import annotations

import logging
from datetime import datetime
from sqlalchemy import update
from sqlmodel import Session, select

from app.engine import audit
from app.engine.dsl import CompiledRule
from app.engine.features import FeatureEngine
from app.engine.graph import MemoryGraphStore
from app.models import Decision, DecisionGuard, EventProjection, RiskCase, RiskEvent

log = logging.getLogger(__name__)


def event_dict(row: RiskEvent) -> dict:
    # MySQL DATETIME 默认可能丢失小数秒；幂等键保留规范化的毫秒时间。
    return {**row.model_dump(), "ts": int(row.idem_key.rsplit(":", 1)[1]) / 1000,
            "event_key": row.idem_key}


def authoritative_snapshot(rt, ev: dict, history: list[dict], list_hits=None) -> dict:
    from app.services.decision import LIST_ENTITY_FIELDS
    past = [e for e in history if float(e["ts"]) <= float(ev["ts"])]
    feats = FeatureEngine.from_events(ev, past)
    graph = MemoryGraphStore()
    for e in past:
        graph.add_event(e)
    feats["related_account_count"] = len(graph.related_accounts(ev["user_id"], hops=2))
    if list_hits is None:
        list_hits = rt.match_lists({kind: ev.get(field) for kind, field in LIST_ENTITY_FIELDS.items()})
    return {"features": feats, "list_hits": list_hits}


def repair_later(rt, session: Session, incoming: dict, history: list[dict]) -> None:
    from app.services.decision import evaluate_event, decision_to_dict
    at = datetime.fromtimestamp(int(float(incoming["ts"])))
    later = session.exec(select(Decision).where(Decision.event_time >= at,
                        Decision.idem_key != incoming["event_key"]).order_by(Decision.event_time, Decision.id)).all()
    by_key = {e["event_key"]: e for e in history}
    for d in later:
        if float(by_key[d.idem_key]["ts"]) < float(incoming["ts"]):
            continue
        job = session.get(EventProjection, d.idem_key)
        if job is None:
            # 旧版未保存完整规则；不能伪造当时的策略。必须人工核对。
            audit.append(session, "system", "decision.recompute_required", "decision", d.id,
                         {"trigger": incoming["event_key"], "reason": "legacy_missing_rule_snapshot"})
            continue
        ev = by_key[d.idem_key]
        snap = authoritative_snapshot(rt, ev, history, list_hits=job.original["list_hits"])
        result = evaluate_event(rt, ev, rules=[CompiledRule(**r) for r in job.rules],
                                snapshot=snap, update_graph=False, settings=job.settings)
        result["rule_version"] = job.original["rule_version"]
        before = decision_to_dict(d)
        fields = ("features", "hits", "score", "decision", "decided_by")
        if all(before[f] == result[f] for f in fields):
            continue
        for field in fields:
            setattr(d, field, result[field])
        session.add(d)
        case = session.exec(select(RiskCase).where(RiskCase.decision_id == d.id)).first()
        if case is None and d.decision != "PASS":
            case = RiskCase(case_no=f"C{d.event_time:%Y%m%d}{d.id:06d}", decision_id=d.id,
                            event_type=d.event_type, user_id=d.user_id, decision=d.decision, score=d.score)
            session.add(case)
            session.flush()
            audit.append(session, "system", "case.create", "case", case.case_no,
                         {"decision_id": d.id, "reason": "late_event_recompute"})
        elif case is not None:
            # 不撤回已经执行的业务动作。保留处置状态，并把修正交给审核员。
            if case.status == "PENDING":
                case.decision, case.score = d.decision, d.score
            audit.append(session, "system", "case.recompute", "case", case.case_no,
                         {"decision_id": d.id, "new_decision": d.decision, "new_score": d.score,
                          "manual_review": case.status != "PENDING"})
        audit.append(session, "system", "decision.recompute", "decision", d.id,
                     {"trigger": incoming["event_key"], "before": before, "after": result})


def project_pending(rt, session: Session, limit: int = 100) -> None:
    """至少一次投递；Redis 按 event_key 去重，图谱 MERGE 不累加计数。"""
    try:
        session.exec(update(DecisionGuard).where(DecisionGuard.id == 1)
                     .values(revision=DecisionGuard.revision + 1))
        jobs = session.exec(select(EventProjection).where(EventProjection.projected == False)
                            .order_by(EventProjection.idem_key).limit(limit)).all()  # noqa: E712
        for job in jobs:
            rt.features.record_and_compute(job.event, accept_late=True)
            rt.graph.add_event(job.event)
            job.projected = True
            session.add(job)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("外部投影未完成，保留任务等待自动重试")


def projection_worker(rt, stop) -> None:
    from app.db.session import get_engine
    while not stop.wait(5):
        try:
            with Session(get_engine()) as session:
                project_pending(rt, session)
        except Exception:
            log.exception("投影重试失败")
