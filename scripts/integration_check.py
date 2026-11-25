"""真实三件套联调自检：对运行中的 8092（MySQL + Redis + Neo4j）逐项验证。

用法（先启动 Redis、Neo4j 和后端）：
    python scripts/integration_check.py
会写入少量以 ITC 开头的测试用户/订单事件，并临时新建、再删除一条不会命中的规则 R-990（停用规则按设计不进知识库）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core import config  # noqa: E402

BASE = f"http://{config.HOST}:{config.PORT}"
KEY = {config.EVENT_API_HEADER: config.EVENT_API_KEYS["ecom-xiaoer"]}
RUN = time.strftime("%H%M%S")
results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, body=None, headers=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'OK ' if ok else 'FAIL'}] {name}  {detail}")


def main() -> int:
    s, h = call("GET", "/health")
    check("健康检查（Redis + Neo4j）", s == 200 and h.get("redis") and h.get("graph") == "neo4j", str(h))

    s, tok = call("POST", "/api/v1/auth/login", {"username": "strategist", "password": config.DEMO_PASSWORD})
    auth = {"Authorization": f"Bearer {tok['token']}"} if s == 200 else {}
    check("登录", s == 200)

    now = time.time()
    user, dev = f"ITC{RUN}_U1", f"ITC{RUN}_D1"
    ev = {"event_type": "order_create", "user_id": user, "order_id": f"ITC{RUN}_O1", "device_id": dev,
          "ip": "10.9.9.9", "phone": "13900000000", "address": "联调路1号", "amount": 99, "ts": now}

    # 1. 接入鉴权
    check("无密钥被拒 401", call("POST", "/api/v1/events", ev)[0] == 401)
    check("错误密钥被拒 401", call("POST", "/api/v1/events", ev, {config.EVENT_API_HEADER: "bad"})[0] == 401)
    s, first = call("POST", "/api/v1/events", ev, KEY)
    check("小二密钥接入 200", s == 200, f"{first.get('decision')} score={first.get('score')}")

    # 2. 幂等：同一 ts 重复调用
    s, dup = call("POST", "/api/v1/events", ev, KEY)
    check("同一事件重复调用命中幂等", s == 200 and dup.get("duplicate") is True
          and dup.get("decision") == first.get("decision"))

    # 3. 售后事件（枚举原因 + 原话 + 金额）
    sale = {"event_type": "after_sale_apply", "user_id": user, "order_id": f"ITC{RUN}_O1", "device_id": dev,
            "ts": now + 1, "payload": {"refund_reason": "not_received", "refund_text": "一直没收到货",
                                       "refund_amount": 99}}
    s, r = call("POST", "/api/v1/events", sale, KEY)
    check("售后事件 200", s == 200, f"{r.get('decision')} user_refund_count_30d={r.get('features', {}).get('user_refund_count_30d')}")

    # 4. 迟到事件重算：先到 t，再补 t-60 同设备订单
    late_dev, t = f"ITC{RUN}_D2", now - 120
    a = {"event_type": "order_create", "user_id": f"ITC{RUN}_U2", "order_id": f"ITC{RUN}_O2",
         "device_id": late_dev, "amount": 10, "ts": t}
    s, ra = call("POST", "/api/v1/events", a, KEY)
    before = ra.get("features", {}).get("device_order_count_1h")
    b = {**a, "user_id": f"ITC{RUN}_U3", "order_id": f"ITC{RUN}_O3", "ts": t - 60}
    call("POST", "/api/v1/events", b, KEY)
    s, ra2 = call("POST", "/api/v1/events", a, KEY)              # 重复请求返回修正后的结果
    after = ra2.get("features", {}).get("device_order_count_1h")
    check("迟到事件重算后续决策", before == 1 and after == 2, f"设备1h订单数 {before} -> {after}")
    if ra2.get("idem_key"):
        s, tr = call("GET", f"/api/v1/decisions/by-key/{ra2['idem_key']}", headers=auth)
        check("回溯带 original 与 corrections", s == 200 and tr.get("original") is not None
              and len(tr.get("corrections", [])) >= 1, f"corrections={len(tr.get('corrections', []))}")

    # 5. 投影：Redis 与 Neo4j 收到数据
    time.sleep(6)
    import redis
    from neo4j import GraphDatabase
    rc = redis.Redis.from_url(config.REDIS_URL)
    keys = [k for k in rc.scan_iter(f"{config.REDIS_PREFIX}:*{dev}*")] + \
           [k for k in rc.scan_iter(f"{config.REDIS_PREFIX}:*{user}*")]
    check("Redis 已有本次事件窗口数据", len(keys) > 0, f"{len(keys)} 个键")
    drv = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
    with drv.session() as gs:
        n = gs.run("MATCH (a:Account {id:$u})--(x) RETURN count(x) AS n", u=user).single()["n"]
    drv.close()
    check("Neo4j 已有账号关联边", n > 0, f"{n} 条关联")

    import pymysql
    db = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER,
                         password=config.MYSQL_PASSWORD, database=config.MYSQL_DATABASE)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM eventprojection WHERE projected = 0")
    pending = cur.fetchone()[0]
    check("投影任务全部完成", pending == 0, f"未完成 {pending}")

    # 6. 规则变更同步知识库
    rule = {"code": "R-990", "name": "联调临时规则", "expression": "amount >= 999999", "score": 1, "enabled": True,
            "description": "integration check"}
    s, _ = call("POST", "/api/v1/rules", rule, auth)
    db.commit(); cur.execute("SELECT COUNT(*) FROM knowledge_doc WHERE doc_code LIKE %s", ("%R-990%",))
    added = cur.fetchone()[0]
    s2, _ = call("DELETE", "/api/v1/rules/R-990", headers=auth)
    db.commit(); cur.execute("SELECT COUNT(*) FROM knowledge_doc WHERE doc_code LIKE %s", ("%R-990%",))
    removed = cur.fetchone()[0]
    check("规则新增/删除同步知识库", s == 201 and s2 in (200, 204) and added >= 1 and removed == 0,
          f"新增后 {added} 条，删除后 {removed} 条")
    db.close()

    fails = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，失败 {len(fails)} 项" + (f"：{fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
