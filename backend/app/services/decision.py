"""实时决策链路：幂等 → 名单过滤 → 特征计算 → 图谱关联 → 规则打分 → 仲裁 → 事务落库建案。

SQL 行锁串行化接入，数据库唯一索引保证幂等；重复事件返回当前持久化决策。
权威特征来自 SQL 历史，外部窗口与图谱在提交后通过持久化任务投影。
idem_key = 业务单号（无订单时用账号）+ 事件类型 + 事件时间戳(ms)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.core import config
from app.engine import audit
from app.engine.dsl import CompiledRule
from app.models import Decision, RiskCase, RiskEvent
from app.services.runtime import Runtime

log = logging.getLogger(__name__)

EVENT_TYPES = ("login", "coupon_receive", "order_create", "order_pay", "after_sale_apply")
PASS, REVIEW, REJECT = "PASS", "REVIEW", "REJECT"
LIST_ENTITY_FIELDS = {"user": "user_id", "device": "device_id", "ip": "ip", "phone": "phone", "address": "address"}


class DuplicateInFlight(Exception):
    """同一事件正在被另一请求处理。"""


def idem_key_of(ev: dict[str, Any]) -> str:
    biz = ev.get("order_id") or ev["user_id"]
    return f"{biz}:{ev['event_type']}:{int(round(float(ev['ts']) * 1000))}"


def arbitrate(score: int) -> str:
    if score >= config.REJECT_THRESHOLD:
        return REJECT
    if score >= config.REVIEW_THRESHOLD:
        return REVIEW
    return PASS


def _idem_redis_key(key: str) -> str:
    # 新版本不读取旧异步落库路径可能留下的未持久化成功缓存。
    return f"{config.REDIS_PREFIX}:idem:v2:{key}"


def evaluate_event(rt: Runtime, ev: dict[str, Any], *, namespace: str = "",
                   rules: list[CompiledRule] | None = None, update_graph: bool = True,
                   snapshot: dict[str, Any] | None = None, settings: dict | None = None) -> dict[str, Any]:
    """纯计算：给定事件算出决策（不做幂等、不落库）。仿真也走这里。"""
    settings = settings or {"review": config.REVIEW_THRESHOLD, "reject": config.REJECT_THRESHOLD, "grey": config.GREYLIST_SCORE}
    t0 = time.perf_counter()
    ev = dict(ev)
    ev["event_key"] = ev.get("event_key") or idem_key_of(ev)

    # 1) 名单
    entities = {etype: ev.get(field) for etype, field in LIST_ENTITY_FIELDS.items()}
    list_hits = snapshot["list_hits"] if snapshot is not None else rt.match_lists(entities)
    kinds = {h["list_type"] for h in list_hits}

    # 2) 特征（名单命中也要写窗口，否则别的账号的聚合特征会失真）
    feats = dict(snapshot["features"]) if snapshot is not None else rt.features.record_and_compute(ev, namespace=namespace)

    # 3) 图谱
    try:
        if snapshot is not None:
            pass  # 历史决策快照包含当时的图谱特征，禁止读取当前状态。
        elif update_graph:
            rt.graph.add_event(ev)
        if snapshot is None:
            feats["related_account_count"] = len(rt.graph.related_accounts(ev["user_id"], hops=2))
    except Exception as e:     # 图谱故障不阻塞实时决策
        log.warning("图谱不可用：%s", e)
        feats["related_account_count"] = None
    feats["in_greylist"] = "grey" in kinds

    hits: list[dict[str, Any]] = []
    if "white" in kinds:
        decision, score, decided_by = PASS, 0, "whitelist"
    elif "black" in kinds:
        decision, score, decided_by = REJECT, 100, "blacklist"
    else:
        rule_set = rt.rules if rules is None else rules
        total = 0
        for rule in rule_set:
            if not rule.applies_to(ev["event_type"]):
                continue
            matched, trace = rule.run(feats)
            if matched:
                total += rule.score
                hits.append({"code": rule.code, "name": rule.name, "score": rule.score,
                             "expression": rule.expression, "conditions": trace})
        if "grey" in kinds:
            total += settings["grey"]
            hits.append({"code": "GREYLIST", "name": "命中灰名单（重点观察）", "score": settings["grey"],
                         "expression": "in_greylist == true", "conditions": []})
        score = max(0, min(100, total))
        decision = REJECT if score >= settings["reject"] else REVIEW if score >= settings["review"] else PASS
        decided_by = "rules"

    return {
        "idem_key": ev["event_key"],
        "event_type": ev["event_type"],
        "user_id": ev["user_id"],
        "decision": decision,
        "score": score,
        "decided_by": decided_by,
        "hits": hits,
        "list_hits": list_hits,
        "features": feats,
        "rule_version": rt.rule_version if rules is None else "simulation",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def decide(rt: Runtime, session: Session, ev: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """线上事务边界：事件、决策、案件、审计持久化后才能返回成功。"""
    from app.services.reliability import authoritative_snapshot, repair_later, project_pending
    from app.models import DecisionGuard, EventProjection
    from sqlalchemy import update
    # ponytail: 全局 SQL 锁 + 扫描事件适合演示规模；扩容需按实体分区和增量物化视图。
    try:
        session.exec(update(DecisionGuard).where(DecisionGuard.id == 1)
                     .values(revision=DecisionGuard.revision + 1))
        key = idem_key_of(ev)
        old = session.exec(select(Decision).where(Decision.idem_key == key)).first()
        if old:
            session.commit()
            return decision_to_dict(old), True
        ev = dict(ev, event_key=key, ts=int(key.rsplit(":", 1)[1]) / 1000)
        rows = session.exec(select(RiskEvent).order_by(RiskEvent.event_time, RiskEvent.id)).all()
        from app.services.reliability import event_dict
        history = [event_dict(row) for row in rows] + [ev]
        snapshot = authoritative_snapshot(rt, ev, history)
        with rt._lock:
            rules = list(rt.rules)
            version = rt.rule_version
        result = evaluate_event(rt, ev, rules=rules, snapshot=snapshot, update_graph=False)
        result["rule_version"] = version
        persist(session, ev, result)
        session.add(EventProjection(idem_key=key, event=ev, rules=[r.to_dict() for r in rules],
                    settings={"review": config.REVIEW_THRESHOLD, "reject": config.REJECT_THRESHOLD,
                              "grey": config.GREYLIST_SCORE}, original=result))
        session.flush()
        repair_later(rt, session, ev, history)
        session.commit()
    except Exception:
        session.rollback()
        raise
    # 投影失败不能回滚已提交的权威记录；后台任务会重试。
    project_pending(rt, session)
    return result, False


def persist(session: Session, ev: dict[str, Any], result: dict[str, Any]) -> Decision:
    """落事件 + 决策快照；REVIEW/REJECT 自动建案并写审计。"""
    event_time = datetime.fromtimestamp(float(ev["ts"]))
    row = RiskEvent(idem_key=result["idem_key"], event_type=ev["event_type"], user_id=ev["user_id"],
                    device_id=ev.get("device_id"), ip=ev.get("ip"), phone=ev.get("phone"),
                    address=ev.get("address"), order_id=ev.get("order_id"),
                    amount=float(ev.get("amount") or 0), event_time=event_time,
                    payload=ev.get("payload") or {})
    session.add(row)
    session.flush()
    d = Decision(event_pk=row.id, idem_key=result["idem_key"], event_type=result["event_type"],
                 user_id=result["user_id"], decision=result["decision"], score=result["score"],
                 decided_by=result["decided_by"], hits=result["hits"], list_hits=result["list_hits"],
                 features=result["features"], rule_version=result["rule_version"],
                 latency_ms=result["latency_ms"], event_time=event_time)
    session.add(d)
    session.flush()
    if d.decision in (REVIEW, REJECT):
        case = RiskCase(case_no=f"C{event_time:%Y%m%d}{d.id:06d}", decision_id=d.id,
                        event_type=d.event_type, user_id=d.user_id, decision=d.decision, score=d.score)
        session.add(case)
        session.flush()
        audit.append(session, "system", "case.create", "case", case.case_no,
                     {"decision_id": d.id, "decision": d.decision, "score": d.score,
                      "rules": [h["code"] for h in d.hits]})
    return d


def decision_to_dict(d: Decision) -> dict[str, Any]:
    return {"idem_key": d.idem_key, "event_type": d.event_type, "user_id": d.user_id,
            "decision": d.decision, "score": d.score, "decided_by": d.decided_by, "hits": d.hits,
            "list_hits": d.list_hits, "features": d.features, "rule_version": d.rule_version,
            "latency_ms": d.latency_ms}
