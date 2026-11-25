"""登录 & 态势大盘。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, func, select

from app.api.schemas import LoginIn
from app.core.security import ROLES, CurrentUser, create_token, current_user, verify_password
from app.db.session import DbSession
from app.models import Decision, RiskCase, SysUser
from app.services.runtime import get_runtime

router = APIRouter(tags=["登录与大盘"])


@router.post("/auth/login", summary="登录")
def login(body: LoginIn, session: Session = DbSession):
    u = session.exec(select(SysUser).where(SysUser.username == body.username)).first()
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "账号或密码错误")
    return {"token": create_token(u.username, u.role, u.display_name),
            "user": {"username": u.username, "name": u.display_name, "role": u.role, "role_cn": ROLES[u.role]}}


@router.get("/auth/me", summary="当前用户")
def me(user: CurrentUser = Depends(current_user)):
    return {"username": user.username, "name": user.name, "role": user.role, "role_cn": ROLES.get(user.role)}


@router.get("/dashboard", summary="态势大盘：拦截率 / 风险分布 / 命中排行 / 趋势")
def dashboard(days: int = Query(7, ge=1, le=90), session: Session = DbSession,
              _: CurrentUser = Depends(current_user)):
    since = datetime.now() - timedelta(days=days)
    rows = session.exec(select(Decision.decision, Decision.score, Decision.hits, Decision.event_type,
                               Decision.event_time, Decision.latency_ms)
                        .where(Decision.event_time >= since)).all()
    total = len(rows)
    dist = Counter(r[0] for r in rows)
    score_buckets = Counter(min(r[1] // 10, 9) for r in rows)
    hits = Counter(h["code"] for r in rows for h in (r[2] or []))
    names = {h["code"]: h["name"] for r in rows for h in (r[2] or [])}
    by_type: dict[str, Counter] = {}
    trend: dict[str, Counter] = {}
    for r in rows:
        by_type.setdefault(r[3], Counter())[r[0]] += 1
        key = r[4].strftime("%m-%d %H:00") if days <= 3 else r[4].strftime("%m-%d")
        trend.setdefault(key, Counter())[r[0]] += 1
    lat = sorted(r[5] for r in rows)
    case_status = dict(session.exec(select(RiskCase.status, func.count()).group_by(RiskCase.status)).all())
    rt = get_runtime()
    return {
        "days": days,
        "total": total,
        "distribution": {k: dist.get(k, 0) for k in ("PASS", "REVIEW", "REJECT")},
        "reject_rate": round(dist["REJECT"] / total, 4) if total else 0,
        "intercept_rate": round((dist["REJECT"] + dist["REVIEW"]) / total, 4) if total else 0,
        "score_histogram": [{"range": f"{b * 10}-{b * 10 + 9 if b < 9 else 100}", "count": score_buckets.get(b, 0)}
                            for b in range(10)],
        "rule_ranking": [{"code": c, "name": names[c], "count": n} for c, n in hits.most_common(10)],
        "by_event_type": {k: dict(v) for k, v in by_type.items()},
        "trend": [{"time": k, **{d: v.get(d, 0) for d in ("PASS", "REVIEW", "REJECT")}} for k, v in sorted(trend.items())],
        "latency_ms": {"avg": round(sum(lat) / total, 2) if total else 0,
                       "p95": lat[int(total * 0.95) - 1] if total else 0},
        "cases": case_status,
        "graph_backend": rt.graph.backend,
        "rule_version": rt.rule_version,
    }
