"""决策级反事实（what-if）：在一条已落库的决策上回答「改哪里结论就会变」。

两层：
1. 规则层：对每条命中规则跑 dsl.counterfactual()，用的是决策当时存下的表达式和特征快照，
   所以规则后来被改过也不影响回溯。得到关键条件和最小改动。
2. 决策层：逐条去掉命中规则重新仲裁，看结论会不会变；再给出离上下两个阈值各差多少分。

只读计算，不写库、不改变任何决策。
"""
from __future__ import annotations

from typing import Any

from app.engine.dsl import DSLError, counterfactual, parse

PASS, REVIEW, REJECT = "PASS", "REVIEW", "REJECT"


def arbitrate(score: int, review: int, reject: int) -> str:
    return REJECT if score >= reject else REVIEW if score >= review else PASS


def _clamp(v: int) -> int:
    return max(0, min(100, v))


def decision_whatif(decision: dict[str, Any], review: int, reject: int) -> dict[str, Any]:
    """decision 需要 decided_by / decision / score / hits / features 五个字段（Decision.model_dump() 即可）。"""
    by = decision.get("decided_by")
    if by != "rules":
        name = {"whitelist": "白名单", "blacklist": "黑名单"}.get(by, by)
        return {"applicable": False, "reason": f"由{name}直接决定，没有经过规则打分，反事实不适用。"}

    feats = decision.get("features") or {}
    hits = decision.get("hits") or []
    raw_total = sum(int(h.get("score", 0)) for h in hits)
    score = int(decision.get("score", 0))
    current = decision.get("decision") or arbitrate(score, review, reject)

    rules = []
    for h in hits:
        expr = h.get("expression") or ""
        item: dict[str, Any] = {"code": h.get("code"), "name": h.get("name"), "score": h.get("score")}
        if not h.get("conditions"):            # 灰名单之类的伪规则，没有可翻转的条件
            item["note"] = "非表达式规则，无条件可翻转"
        else:
            try:
                cf = counterfactual(parse(expr), feats)
            except DSLError as e:
                item["note"] = f"表达式无法解析：{e}"
            else:
                item.update(matched=cf["matched"], critical=cf["critical"],
                            minimal_change=cf["minimal_change"],
                            conditions=[{"cid": c["cid"], "condition": c["condition"], "critical": c["critical"],
                                         "relative": c.get("relative")} for c in cf["conditions"]])
        new_score = _clamp(raw_total - int(h.get("score", 0)))
        new_dec = arbitrate(new_score, review, reject)
        item["without"] = {"score": new_score, "decision": new_dec, "changes": new_dec != current}
        rules.append(item)

    # 离阈值的距离：往下降一档要少多少分，往上升一档要多多少分
    lower = {REJECT: reject, REVIEW: review}.get(current)
    upper = {PASS: review, REVIEW: reject}.get(current)
    margin = {
        "down": ({"need": score - lower + 1, "to": arbitrate(lower - 1, review, reject)} if lower is not None else None),
        "up": ({"need": upper - score, "to": arbitrate(upper, review, reject)} if upper is not None else None),
    }
    pivotal = [r["code"] for r in rules if r["without"]["changes"]]
    if pivotal:
        summary = "去掉 " + " 或 ".join(pivotal) + f" 中任意一条，结论就不再是 {current}。"
    elif rules and current == PASS:
        summary = f"当前已是 PASS（{score} 分），单独去掉任何一条命中规则结论都不变。"
    elif rules:
        summary = f"单独去掉任何一条命中规则，结论都还是 {current}：这是多条规则叠加的结果。"
    else:
        summary = f"没有命中规则，风险分 0，结论 {current}。"
    return {"applicable": True, "current": current, "score": score, "thresholds": {"review": review, "reject": reject},
            "margin": margin, "pivotal_rules": pivotal, "summary": summary, "rules": rules}
