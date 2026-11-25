"""首次启动初始化：系统账号、默认规则、演示数据。

演示数据全部通过真实决策链路灌入（decide → persist），所以窗口特征、图谱、决策快照、
案件、审计链都是真实算出来的，不是直接往表里插结果。
场景：正常用户 / 羊毛党团伙领券 / 黄牛囤货 / 刷单 / 恶意退款。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

from sqlmodel import Session, func, select

from app.core import config
from app.core.security import hash_password
from app.engine import audit
from app.models import RiskEvent, Rule, SysUser
from app.services import decision as dsvc
from app.services import rag
from app.services.runtime import Runtime

log = logging.getLogger(__name__)

DEMO_USERS = [("admin", "管理员", "admin"), ("strategist", "策略师小王", "strategist"),
              ("auditor", "审核员小李", "auditor")]

#     code, 名称, 事件类型, 表达式, 分值, 说明
DEFAULT_RULES: list[tuple[str, str, list[str], str, int, str]] = [
    ("R-001", "新账号高频领券", ["coupon_receive"],
     "user_coupon_count_1h >= 3 AND is_new_account == true", 40, "羊毛党：注册不满 7 天，1 小时内领券 ≥3 次"),
    ("R-002", "同设备多账号领券", ["coupon_receive"],
     "device_coupon_count_24h >= 5 AND device_account_count_24h >= 3", 50, "羊毛党：一台设备 24 小时内被 ≥3 个账号用来领券"),
    ("R-003", "团伙关联账号多", [],
     "related_account_count >= 5", 30, "图谱：2 跳内共用设备/IP/手机/地址的其他账号 ≥5 个"),
    ("R-004", "同设备高频下单", ["order_create"],
     "device_order_count_1h > 5", 45, "黄牛：同一设备 1 小时内下单超过 5 次"),
    ("R-005", "同地址多账号下单", ["order_create", "order_pay"],
     "address_account_count_7d >= 4", 35, "黄牛：同一收货地址 7 天内出现 ≥4 个账号"),
    ("R-006", "新账号大额订单", ["order_create"],
     "is_new_account == true AND amount >= 3000", 25, "新账号首单金额 ≥3000 元"),
    ("R-007", "同 IP 多账号支付", ["order_pay"],
     "ip_account_count_24h >= 5", 30, "刷单：同一 IP 24 小时内 ≥5 个账号"),
    ("R-008", "恶意退款：多次未收到货", ["after_sale_apply"],
     "user_refund_count_30d >= 3 AND refund_reason == 'not_received'", 60,
     "30 天内同账号发起「未收到货」退款 ≥3 次，判定为恶意退款风险"),
    ("R-009", "收货手机号退款率高", ["after_sale_apply"],
     "phone_refund_rate_24h >= 0.6 AND phone_refund_count_24h >= 2", 30, "同收货手机号 24 小时退款率 ≥60%"),
    ("R-010", "账号高频下单", ["order_create"],
     "user_order_count_24h >= 10", 30, "刷单：同账号 24 小时下单 ≥10 次"),
    ("R-011", "新账号高额退款", ["after_sale_apply"],
     "refund_amount >= 2000 AND account_age_days < 30", 20, "注册不满 30 天，单笔退款 ≥2000 元"),
    ("R-012", "同设备多账号登录", ["login"],
     "device_account_count_24h >= 5", 40, "同一设备 24 小时内登录 ≥5 个账号（养号/批量登录）"),
]


def ensure_users(session: Session) -> None:
    for username, name, role in DEMO_USERS:
        if not session.exec(select(SysUser).where(SysUser.username == username)).first():
            session.add(SysUser(username=username, display_name=name, role=role,
                                password_hash=hash_password(config.DEMO_PASSWORD)))


def ensure_rules(session: Session) -> None:
    if session.exec(select(func.count()).select_from(Rule)).one():
        return
    for code, name, types, expr, score, desc in DEFAULT_RULES:
        session.add(Rule(code=code, name=name, event_types=types, expression=expr, score=score,
                         description=desc))
    session.flush()
    audit.append(session, "system", "rule.init", "rule", "*", {"count": len(DEFAULT_RULES)})


# ------------------------------------------------------------------ 演示数据
def generate_demo_events(now: float | None = None, seed: int = 42) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    now = now or time.time()
    day = 86400
    evs: list[dict[str, Any]] = []
    seq = 0

    def add(etype: str, uid: str, ts: float, **kw):
        nonlocal seq
        seq += 1
        payload = kw.pop("payload", {})
        payload.setdefault("register_ts", kw.pop("register_ts"))
        order_id = kw.pop("order_id", None)
        if etype in ("order_create", "order_pay", "after_sale_apply") and not order_id:
            order_id = f"O{seq:06d}"
        evs.append({"event_type": etype, "user_id": uid, "ts": round(ts, 3), "order_id": order_id,
                    "payload": payload, **kw})

    cities = ["杭州市西湖区文三路", "上海市浦东新区张江路", "北京市海淀区中关村", "深圳市南山区科技园",
              "成都市高新区天府大道", "武汉市洪山区光谷", "南京市鼓楼区中山路", "广州市天河区体育西路"]

    # 正常用户：40 个，分散在 3 天里
    for i in range(40):
        uid = f"u{1000 + i}"
        reg = now - rnd.randint(60, 900) * day
        dev, ip = f"dev-n{i:03d}", f"10.{rnd.randint(1, 250)}.{rnd.randint(1, 250)}.{i + 1}"
        phone, addr = f"138{rnd.randint(10000000, 99999999)}", f"{rnd.choice(cities)}{rnd.randint(1, 300)}号"
        base = dict(device_id=dev, ip=ip, phone=phone, address=addr, register_ts=reg)
        for _ in range(rnd.randint(1, 3)):
            t = now - rnd.uniform(0.1, 3) * day
            add("login", uid, t, **base)
            if rnd.random() < 0.6:
                add("coupon_receive", uid, t + 30, payload={"coupon_amount": rnd.choice([5, 10, 20])}, **base)
            amt = round(rnd.uniform(30, 900), 2)
            oid = f"N{i:03d}{rnd.randint(1000, 9999)}"
            add("order_create", uid, t + 120, order_id=oid, amount=amt, **base)
            add("order_pay", uid, t + 180, order_id=oid, amount=amt, **base)
            if rnd.random() < 0.1:
                add("after_sale_apply", uid, t + 2 * day if t + 2 * day < now else now - 60, order_id=oid,
                    payload={"refund_reason": "quality", "refund_amount": amt}, **base)

    # 羊毛党团伙：12 个新号、2 台设备、同一 IP，30 分钟内密集领券
    t0 = now - 0.5 * day
    for i in range(12):
        uid = f"w{2000 + i}"
        base = dict(device_id=f"dev-wool-{i % 2}", ip="223.104.18.77", phone=f"170{55500000 + i}",
                    address=f"湖南省长沙市岳麓区麓谷{i % 3 + 1}栋", register_ts=t0 - rnd.uniform(0.5, 3) * day)
        add("login", uid, t0 + i * 60, **base)
        for k in range(4):
            add("coupon_receive", uid, t0 + i * 60 + 10 + k * 40, payload={"coupon_amount": 50}, **base)

    # 黄牛：4 个账号、同一设备、同一地址，1 小时内抢购
    t1 = now - 1.2 * day
    for k in range(12):
        uid = f"s{3000 + k % 4}"
        add("order_create", uid, t1 + k * 240, device_id="dev-scalper-01", ip="114.88.20.9",
            phone=f"139{77700000 + k % 4}", address="浙江省义乌市福田街道商城大道88号",
            amount=1999.0, register_ts=now - 200 * day)

    # 刷单：6 个账号同一 IP 支付
    t2 = now - 2.1 * day
    for i in range(6):
        uid = f"b{4000 + i}"
        oid = f"B{i:04d}"
        base = dict(device_id=f"dev-brush-{i}", ip="61.152.33.200", phone=f"137{66600000 + i}",
                    address=f"江苏省苏州市工业园区星湖街{i + 1}号", register_ts=now - 40 * day)
        add("order_create", uid, t2 + i * 300, order_id=oid, amount=88.0, **base)
        add("order_pay", uid, t2 + i * 300 + 60, order_id=oid, amount=88.0, **base)

    # 恶意退款：一个账号 25 天内 4 次「未收到货」高额退款
    base = dict(device_id="dev-refund-01", ip="27.18.6.31", phone="13512345678",
                address="广东省东莞市虎门镇连升路18号", register_ts=now - 20 * day)
    for k in range(4):
        t = now - (24 - k * 7.5) * day if k < 3 else now - 0.2 * day
        oid = f"R{k:04d}"
        add("order_create", "r5000", t - 3 * day, order_id=oid, amount=2399.0, **base)
        add("after_sale_apply", "r5000", t, order_id=oid,
            payload={"refund_reason": "not_received", "refund_amount": 2399.0}, **base)

    evs.sort(key=lambda e: e["ts"])
    return evs


def seed_demo(session: Session, rt: Runtime, events: list[dict[str, Any]] | None = None) -> int:
    rt.features.flush_all()
    rt.graph.clear()
    n = 0
    for ev in events or generate_demo_events():
        result, dup = dsvc.decide(rt, session, ev)
        if not dup:
            n += 1
    return n


def bootstrap(session: Session, rt: Runtime, with_demo: bool = True) -> None:
    ensure_users(session)
    ensure_rules(session)
    session.flush()
    rag.ensure_knowledge(session)
    rt.reload_rules(session)
    rt.reload_lists(session)
    if with_demo and not session.exec(select(func.count()).select_from(RiskEvent)).one():
        n = seed_demo(session, rt)
        log.info("已灌入演示事件 %d 条", n)
    elif rt.graph.backend == "memory":
        rebuild_graph(session, rt)


def rebuild_graph(session: Session, rt: Runtime) -> None:
    """Neo4j 不可用降级到内存图时，从事件表重建图谱。"""
    for e in session.exec(select(RiskEvent)):
        rt.graph.add_event({"user_id": e.user_id, "device_id": e.device_id, "ip": e.ip,
                            "phone": e.phone, "address": e.address})
