import tests  # noqa: F401  必须第一行
import time
import unittest

import fakeredis
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import session_scope
from app.engine.graph import MemoryGraphStore
from app.main import create_app
from app.models import Decision, RiskCase
from app.services.runtime import init_runtime


def make_client(db_name: str, with_demo: bool) -> TestClient:
    tests.fresh_db(db_name)
    init_runtime(fakeredis.FakeRedis(decode_responses=True), MemoryGraphStore())
    app = create_app()
    app.state.runtime_ready = True
    app.state.with_demo = with_demo
    return TestClient(app)


def login(c: TestClient, user: str) -> dict:
    r = c.post("/api/v1/auth/login", json={"username": user, "password": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class DemoScenarioTest(unittest.TestCase):
    """演示数据走完整链路后，各欺诈场景必须被识别出来。"""

    @classmethod
    def setUpClass(cls):
        cls.c = make_client("demo", with_demo=True)
        cls.c.__enter__()
        cls.h = login(cls.c, "admin")

    @classmethod
    def tearDownClass(cls):
        cls.c.__exit__(None, None, None)

    def _decisions(self, user_prefix: str):
        with session_scope() as s:
            rows = s.exec(select(Decision).where(Decision.user_id.startswith(user_prefix))).all()
            return [(r.user_id, r.event_type, r.decision, r.score, [h["code"] for h in r.hits]) for r in rows]

    def test_health(self):
        r = self.c.get("/health").json()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["graph"], "memory")

    def test_wool_gang_rejected(self):
        rows = self._decisions("w2")
        rejected = [r for r in rows if r[1] == "coupon_receive" and r[2] == "REJECT"]
        self.assertGreaterEqual(len(rejected), 10)
        self.assertTrue(any({"R-001", "R-002", "R-003"} <= set(r[4]) for r in rejected))

    def test_scalper_rejected(self):
        rows = self._decisions("s3")
        self.assertTrue(any(r[2] == "REJECT" and {"R-004", "R-005"} <= set(r[4]) for r in rows))

    def test_brushing_flagged(self):
        rows = self._decisions("b4")
        self.assertTrue(any(r[1] == "order_pay" and r[2] != "PASS" and "R-007" in r[4] for r in rows))

    def test_malicious_refund_rejected(self):
        rows = [r for r in self._decisions("r5000") if r[1] == "after_sale_apply"]
        self.assertEqual(rows[-1][2], "REJECT")
        self.assertIn("R-008", rows[-1][4])

    def test_normal_users_mostly_pass(self):
        rows = self._decisions("u1")
        passed = sum(r[2] == "PASS" for r in rows)
        self.assertGreaterEqual(passed / len(rows), 0.95)

    def test_dashboard(self):
        d = self.c.get("/api/v1/dashboard?days=90", headers=self.h).json()
        self.assertGreater(d["total"], 100)
        self.assertGreater(d["distribution"]["REJECT"], 0)
        self.assertEqual(sum(b["count"] for b in d["score_histogram"]), d["total"])
        self.assertTrue(d["rule_ranking"])
        self.assertEqual(d["cases"]["PENDING"], d["distribution"]["REJECT"] + d["distribution"]["REVIEW"])

    def test_gangs(self):
        g = self.c.get("/api/v1/graph/gangs?min_size=4", headers=self.h).json()["gangs"]
        members = [set(x["accounts"]) for x in g]
        self.assertTrue(any({f"w{2000 + i}" for i in range(12)} <= m for m in members))

    def test_trace(self):
        lst = self.c.get("/api/v1/decisions?decision=REJECT&size=1", headers=self.h).json()
        d = self.c.get(f"/api/v1/decisions/{lst['items'][0]['id']}", headers=self.h).json()
        self.assertTrue(d["case_no"])
        self.assertIn("累计", d["explain"])
        hit = d["decision"]["hits"][0]
        self.assertTrue(hit["conditions"] and "actual" in hit["conditions"][0])
        w = d["whatif"]
        self.assertTrue(w["applicable"])
        self.assertEqual(w["current"], "REJECT")
        self.assertEqual([r["code"] for r in w["rules"]], [h["code"] for h in d["decision"]["hits"]])
        expr_rules = [r for r in w["rules"] if "matched" in r]
        self.assertTrue(expr_rules and all(r["matched"] for r in expr_rules))   # 用快照重算必须仍命中
        case = self.c.get(f"/api/v1/cases/{d['case_no']}", headers=self.h).json()
        self.assertEqual(case["whatif"]["summary"], w["summary"])

    def test_validate_returns_counterfactual(self):
        r = self.c.post("/api/v1/rules/validate", headers=self.h, json={
            "expression": "device_order_count_1h >= 5 AND amount > 1000",
            "sample": {"device_order_count_1h": 6, "amount": 3999}}).json()
        self.assertTrue(r["matched"])
        self.assertEqual(r["counterfactual"]["minimal_change"]["field"], "device_order_count_1h")

    def test_simulation_draft_rule(self):
        h = login(self.c, "strategist")
        body = {"draft": {"code": "R-099", "name": "试验", "expression": "user_coupon_count_1h >= 2",
                          "score": 80, "event_types": ["coupon_receive"]}}
        r = self.c.post("/api/v1/simulate", json=body, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()
        self.assertEqual(res["total"], self.c.get("/api/v1/dashboard?days=90", headers=self.h).json()["total"])
        self.assertIn("R-099", [x["code"] for x in res["rule_hits"]])
        bad = self.c.post("/api/v1/simulate", json={"draft": {**body["draft"], "expression": "x >"}}, headers=h)
        self.assertEqual(bad.status_code, 422)
        rt_keys = list(__import__("app.services.runtime", fromlist=["x"]).get_runtime().redis.scan_iter("risk:sim:*"))
        self.assertEqual(rt_keys, [])

    def test_audit_chain_intact(self):
        self.assertTrue(self.c.get("/api/v1/audit/verify", headers=self.h).json()["ok"])

    def _reject_case(self) -> str:
        with session_scope() as s:
            return s.exec(select(RiskCase).where(RiskCase.decision == "REJECT")).first().case_no

    def test_knowledge_search(self):
        r = self.c.get("/api/v1/knowledge/search", params={"q": "恶意退款 未收到货"}, headers=self.h).json()
        self.assertTrue(r["items"])
        self.assertEqual(self.c.get("/api/v1/knowledge/search", params={"q": "x"}).status_code, 401)

    def test_assist_retrieval_mode(self):
        no = self._reject_case()
        r = self.c.post(f"/api/v1/cases/{no}/assist", headers=self.h).json()
        self.assertEqual(r["mode"], "retrieval")
        self.assertTrue(r["references"])
        codes = {x["doc_code"] for x in r["references"]}
        self.assertTrue(any(f"[{c}]" in r["answer"] for c in codes), "检索模式答案必须带出处")
        self.assertEqual(self.c.post("/api/v1/cases/NOPE/assist", headers=self.h).status_code, 404)

    def test_assist_llm_mode_and_fallback(self):
        import json as _json
        from unittest import mock
        from app.core import config

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return _json.dumps({"choices": [{"message": {"content": "命中规则[1]，参考判例[2]。"}}]}).encode()

        no = self._reject_case()
        with mock.patch.object(config, "RAG_LLM_URL", "http://fake/v1"):
            with mock.patch("urllib.request.urlopen", return_value=Resp()):
                r = self.c.post(f"/api/v1/cases/{no}/assist", json={"question": "为什么拒绝？"}, headers=self.h).json()
            self.assertEqual(r["mode"], "llm")
            refs = r["references"]
            self.assertIn(f"[{refs[0]['doc_code']}]", r["answer"])      # [n] 已映射回文档编号
            with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("down")):
                r = self.c.post(f"/api/v1/cases/{no}/assist", headers=self.h).json()
            self.assertEqual(r["mode"], "retrieval")                       # 大模型故障自动降级


class WorkflowTest(unittest.TestCase):
    """空库：事件接入、幂等、规则增改、名单、案件闭环、权限。"""

    def setUp(self):
        self.c = make_client(f"wf{time.time_ns()}", with_demo=False)
        self.c.__enter__()
        self.admin = login(self.c, "admin")
        self.strat = login(self.c, "strategist")
        self.aud = login(self.c, "auditor")
        self.now = time.time()

    def tearDown(self):
        self.c.__exit__(None, None, None)

    def post(self, **kw):
        body = {"event_type": "order_create", "user_id": "u1", "ts": self.now, "order_id": "O1",
                "device_id": "D1", "amount": 100, **kw}
        return self.c.post("/api/v1/events", json=body, headers=tests.EVENT_HEADERS)

    def test_idempotent(self):
        a = self.post().json()
        b = self.post().json()
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["features"], b["features"])
        c = self.post(order_id="O2", ts=self.now + 1).json()
        self.assertEqual(c["features"]["device_order_count_1h"], 2)     # 重复事件没有被重复计数
        with session_scope() as s:
            self.assertEqual(len(s.exec(select(Decision)).all()), 2)

    def test_event_auth(self):
        body = {"event_type": "order_create", "user_id": "u1", "ts": self.now, "order_id": "OA",
                "device_id": "D1", "amount": 100}
        self.assertEqual(self.c.post("/api/v1/events", json=body).status_code, 401)            # 无凭证
        self.assertEqual(self.c.post("/api/v1/events", json=body,
                                     headers={"X-Risk-Api-Key": "wrong"}).status_code, 401)  # 错误密钥
        bad = {"Authorization": "Bearer a.b.c"}
        self.assertEqual(self.c.post("/api/v1/events", json=body, headers=bad).status_code, 401)
        with session_scope() as s:
            self.assertEqual(len(s.exec(select(Decision)).all()), 0)                         # 被拒的请求不落库
        self.assertEqual(self.c.post("/api/v1/events", json=body, headers=self.aud).status_code, 200)  # 控制台登录用户
        r = self.c.post("/api/v1/events", json={**body, "order_id": "OB"}, headers=tests.EVENT_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)                                         # 业务系统密钥

    def test_validation(self):
        self.assertEqual(self.post(event_type="hack").status_code, 422)
        self.assertEqual(self.post(order_id=None).status_code, 422)
        self.assertEqual(self.post(ts=self.now + 3600).status_code, 422)
        r = self.c.post("/api/v1/events", json={"event_type": "after_sale_apply", "user_id": "u", "order_id": "O"},
                        headers=tests.EVENT_HEADERS)
        self.assertEqual(r.status_code, 422)
        ms = self.post(ts=int(self.now * 1000)).json()                    # 毫秒自动换算
        self.assertFalse(ms["duplicate"])

    def test_commit_failure_is_retryable_and_atomic(self):
        from unittest.mock import patch
        from sqlalchemy.exc import OperationalError
        from sqlmodel import Session
        self.c.post("/api/v1/rules", json={"code": "R-199", "name": "reject",
                    "expression": "amount >= 1", "score": 90}, headers=self.strat)
        with patch.object(Session, "commit", side_effect=OperationalError("commit", {}, Exception("down"))):
            self.assertEqual(self.post().status_code, 503)
        with session_scope() as s:
            self.assertEqual(len(s.exec(select(Decision)).all()), 0)
            self.assertEqual(len(s.exec(select(RiskCase)).all()), 0)
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["duplicate"])
        self.assertEqual(r.json()["features"]["device_order_count_1h"], 1)
        self.assertTrue(self.post().json()["duplicate"])
        with session_scope() as s:
            self.assertEqual(len(s.exec(select(Decision)).all()), 1)
            self.assertEqual(len(s.exec(select(RiskCase)).all()), 1)
        self.assertTrue(self.c.get("/api/v1/audit/verify", headers=self.admin).json()["ok"])

    def test_simulation_never_reads_live_state(self):
        from unittest.mock import patch
        from app.services.runtime import get_runtime
        self.post()
        rt = get_runtime()
        with patch.object(rt, "match_lists", side_effect=AssertionError("live lists")), \
             patch.object(rt.graph, "related_accounts", side_effect=AssertionError("live graph")), \
             patch.object(rt.features, "record_and_compute", side_effect=AssertionError("live features")):
            r = self.c.post("/api/v1/simulate", json={}, headers=self.strat)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["changed"], 0)
        self.assertEqual(r.json()["mode"], "historical_feature_snapshot")

    def test_rule_knowledge_lifecycle(self):
        from app.models import KnowledgeDoc
        def doc():
            with session_scope() as s:
                return s.exec(select(KnowledgeDoc).where(KnowledgeDoc.doc_code == "R-199")).first()
        self.c.post("/api/v1/rules", json={"code": "R-199", "name": "test",
                    "expression": "amount >= 1", "score": 30}, headers=self.strat)
        self.assertIn("加 30 分", doc().content)
        self.c.get("/api/v1/knowledge/search", params={"q": "R-199"}, headers=self.admin)
        self.c.put("/api/v1/rules/R-199", json={"score": 65}, headers=self.strat)
        r = self.c.get("/api/v1/knowledge/search", params={"q": "R-199"}, headers=self.admin).json()
        self.assertTrue(any(x["doc_code"] == "R-199" and "加 65 分" in x["content"] for x in r["items"]))
        self.c.put("/api/v1/rules/R-199", json={"enabled": False}, headers=self.strat)
        self.assertIsNone(doc())
        self.c.put("/api/v1/rules/R-199", json={"enabled": True}, headers=self.strat)
        self.assertIsNotNone(doc())
        self.c.delete("/api/v1/rules/R-199", headers=self.strat)
        self.assertIsNone(doc())

    def test_rule_lifecycle_and_case_loop(self):
        # 权限：审核员不能改规则
        body = {"code": "R-100", "name": "测试大额", "expression": "amount >= 5000", "score": 90,
                "event_types": ["order_create"]}
        self.assertEqual(self.c.post("/api/v1/rules", json=body, headers=self.aud).status_code, 403)
        self.assertEqual(self.c.post("/api/v1/rules", json={**body, "expression": "nope > 1"},
                                     headers=self.strat).status_code, 422)
        self.assertEqual(self.c.post("/api/v1/rules", json=body, headers=self.strat).status_code, 201)

        r = self.post(amount=6000).json()
        self.assertEqual((r["decision"], r["score"]), ("REJECT", 90))
        with session_scope() as s:
            case_no = s.exec(select(RiskCase)).one().case_no

        # 改分值 → 版本 +1，立即生效
        u = self.c.put("/api/v1/rules/R-100", json={"score": 65}, headers=self.strat).json()
        self.assertEqual(u["version"], 2)
        r2 = self.post(order_id="O9", ts=self.now + 5, amount=6000, device_id="D9", user_id="u9").json()
        self.assertEqual(r2["decision"], "REVIEW")

        # 案件闭环
        p = f"/api/v1/cases/{case_no}"
        self.assertEqual(self.c.post(p + "/dispose", json={"actions": ["blacklist_user"], "conclusion": "fraud"},
                                     headers=self.aud).status_code, 409)          # 未认领不能处置
        self.assertEqual(self.c.post(p + "/claim", headers=self.aud).json()["status"], "IN_REVIEW")
        self.assertEqual(self.c.post(p + "/dispose", json={"actions": ["release", "blacklist_user"],
                                                          "conclusion": "fraud"}, headers=self.aud).status_code, 409)
        d = self.c.post(p + "/dispose", json={"actions": ["block_order", "blacklist_user"], "conclusion": "fraud",
                                              "note": "确认欺诈"}, headers=self.aud).json()
        self.assertEqual(d["case"]["status"], "DISPOSED")
        self.assertEqual(len(d["namelist_added"]), 1)
        self.assertEqual(self.c.post(p + "/archive", headers=self.aud).json()["status"], "ARCHIVED")

        # 拉黑后再来 → 名单直接拒绝
        r3 = self.post(order_id="O10", ts=self.now + 10, amount=1).json()
        self.assertEqual((r3["decision"], r3["decided_by"], r3["score"]), ("REJECT", "blacklist", 100))

        detail = self.c.get(p, headers=self.admin).json()
        self.assertEqual([a["action"] for a in detail["audit"]],
                         ["case.create", "case.claim", "case.dispose", "case.archive"])
        self.assertTrue(self.c.get("/api/v1/audit/verify", headers=self.admin).json()["ok"])

    def test_write_committed_before_response(self):
        """写接口必须先提交再返回：客户端一拿到响应，别的连接就能读到新状态（认领后立即处置不能 409）。"""
        self.c.post("/api/v1/rules", json={"code": "R-100", "name": "x", "expression": "amount >= 10",
                                           "score": 90}, headers=self.strat)
        self.post()
        with session_scope() as s:
            case_no = s.exec(select(RiskCase)).one().case_no
        seen = []

        async def probe(scope, receive, send):
            async def send_wrapper(message):
                if message["type"] == "http.response.start":   # 响应头发出的那一刻，用另一个会话读库
                    with session_scope() as s:
                        seen.append(s.exec(select(RiskCase.status).where(RiskCase.case_no == case_no)).one())
                await send(message)
            await self.c.app(scope, receive, send_wrapper)

        r = TestClient(probe).post(f"/api/v1/cases/{case_no}/claim", headers=self.aud)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, ["IN_REVIEW"])

    def test_whitelist_and_greylist(self):
        self.c.post("/api/v1/rules", json={"code": "R-100", "name": "x", "expression": "amount >= 10",
                                           "score": 50}, headers=self.strat)
        self.c.post("/api/v1/namelists", json={"list_type": "grey", "entity_type": "device", "value": "D1"},
                    headers=self.strat)
        g = self.post().json()
        self.assertEqual((g["score"], g["decision"]), (70, "REVIEW"))
        self.c.post("/api/v1/namelists", json={"list_type": "white", "entity_type": "user", "value": "u2"},
                    headers=self.strat)
        w = self.post(user_id="u2", order_id="O2").json()
        self.assertEqual((w["decision"], w["decided_by"]), ("PASS", "whitelist"))

    def test_auth_required(self):
        self.assertEqual(self.c.get("/api/v1/dashboard").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/dashboard", headers={"Authorization": "Bearer x.y.z"}).status_code, 401)
        self.assertEqual(self.c.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"}).status_code,
                         401)


if __name__ == "__main__":
    unittest.main()
