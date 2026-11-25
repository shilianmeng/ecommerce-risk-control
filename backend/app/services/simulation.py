"""规则仿真：在历史决策保存的特征和名单快照上评估候选规则，验证命中 / 误伤。

隔离：不读取当前图谱、名单或实时窗口，不落库、不建案。
标签来源：已处置案件的人工结论（fraud=确认欺诈，normal=误判）。
注意：仅评估规则变更，不模拟策略反馈对后续事件的影响，也不重新计算旧快照中的错误特征。
"""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlmodel import Session, select

from app.api.schemas import SimulateIn
from app.engine.dsl import CompiledRule
from app.models import Decision, RiskCase, RiskEvent, Rule
from app.services.decision import PASS, evaluate_event
from app.services.runtime import Runtime


def build_rule_set(session: Session, req: SimulateIn) -> list[CompiledRule]:
    if req.rules is not None:
        return [CompiledRule(r.code, r.name or r.code, r.expression, r.score, r.event_types) for r in req.rules]
    rows = session.exec(select(Rule).where(Rule.enabled == True).order_by(Rule.priority, Rule.code)).all()  # noqa: E712
    rules = {r.code: CompiledRule(r.code, r.name, r.expression, r.score, r.event_types, r.priority) for r in rows}
    if req.draft:
        d = req.draft
        rules[d.code] = CompiledRule(d.code, d.name or d.code, d.expression, d.score, d.event_types)
    return list(rules.values())


def run(session: Session, rt: Runtime, req: SimulateIn) -> dict[str, Any]:
    rules = build_rule_set(session, req)        # 表达式有错在这里直接抛 DSLError

    stmt = select(RiskEvent, Decision).join(Decision, Decision.event_pk == RiskEvent.id)
    if req.start:
        stmt = stmt.where(RiskEvent.event_time >= req.start)
    if req.end:
        stmt = stmt.where(RiskEvent.event_time <= req.end)
    if req.event_types:
        stmt = stmt.where(RiskEvent.event_type.in_(req.event_types))
    rows = session.exec(stmt.order_by(RiskEvent.event_time, RiskEvent.id).limit(req.limit)).all()

    labels = {c.decision_id: c.conclusion for c in session.exec(
        select(RiskCase).where(RiskCase.conclusion.is_not(None))).all()}

    ns = f"sim:{uuid.uuid4().hex[:10]}"
    old_dist, new_dist, rule_hits = Counter(), Counter(), Counter()
    fraud_total = fraud_caught = normal_total = normal_flagged = newly_flagged = changed = 0
    samples: list[dict[str, Any]] = []
    try:
        for ev_row, dec in rows:
            ev = {"event_type": ev_row.event_type, "user_id": ev_row.user_id, "ts": ev_row.event_time.timestamp(),
                  "device_id": ev_row.device_id, "ip": ev_row.ip, "phone": ev_row.phone,
                  "address": ev_row.address, "order_id": ev_row.order_id, "amount": ev_row.amount,
                  "payload": ev_row.payload or {}, "event_key": ev_row.idem_key}
            res = evaluate_event(rt, ev, namespace=ns, rules=rules, update_graph=False,
                                 snapshot={"features": dec.features, "list_hits": dec.list_hits})
            old_dist[dec.decision] += 1
            new_dist[res["decision"]] += 1
            for h in res["hits"]:
                rule_hits[h["code"]] += 1
            flagged = res["decision"] != PASS
            label = labels.get(dec.id)
            if label == "fraud":
                fraud_total += 1
                fraud_caught += flagged
            elif label == "normal":
                normal_total += 1
                normal_flagged += flagged
            if flagged and dec.decision == PASS:
                newly_flagged += 1
            if res["decision"] != dec.decision:
                changed += 1
                if len(samples) < 50:
                    samples.append({"idem_key": ev_row.idem_key, "event_type": ev_row.event_type,
                                    "user_id": ev_row.user_id, "event_time": ev_row.event_time,
                                    "old": {"decision": dec.decision, "score": dec.score},
                                    "new": {"decision": res["decision"], "score": res["score"]},
                                    "hits": res["hits"], "label": label})
    finally:
        rt.features.drop_namespace(ns)

    total = len(rows)
    return {
        "mode": "historical_feature_snapshot",
        "limitations": "固定历史特征与名单快照，仅比较规则变更；不模拟策略反馈对后续事件的影响。",
        "total": total,
        "rule_count": len(rules),
        "old_distribution": dict(old_dist),
        "new_distribution": dict(new_dist),
        "changed": changed,
        "newly_flagged": newly_flagged,
        "rule_hits": [{"code": k, "count": v} for k, v in rule_hits.most_common()],
        "labeled": {
            "fraud_total": fraud_total, "fraud_caught": fraud_caught,
            "recall": round(fraud_caught / fraud_total, 4) if fraud_total else None,
            "normal_total": normal_total, "false_hits": normal_flagged,
            "false_hit_rate": round(normal_flagged / normal_total, 4) if normal_total else None,
        },
        "samples": samples,
    }
