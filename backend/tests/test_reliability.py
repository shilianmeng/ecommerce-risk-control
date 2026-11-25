import tests  # noqa: F401
import time
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from tests.test_api import make_client, login
from app.db.session import session_scope
from app.models import Decision, EventProjection, RiskCase, RiskEvent
from app.services.reliability import project_pending, projection_worker
from app.services.runtime import get_runtime


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.client = make_client(f"reliability{time.time_ns()}", with_demo=False)
        self.client.__enter__()
        self.auth = login(self.client, "strategist")
        self.rt = get_runtime()
        self.now = round(time.time() - 100, 3)

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def post(self, order, offset=0, user="u1"):
        return self.client.post("/api/v1/events", json={"event_type": "order_create",
            "user_id": user, "order_id": order, "device_id": "D1", "ts": self.now + offset}, headers=tests.EVENT_HEADERS)

    def rule(self):
        response = self.client.post("/api/v1/rules", headers=self.auth, json={
            "code": "R-199", "name": "late event", "expression": "device_order_count_1h >= 2",
            "score": 90, "event_types": ["order_create"]})
        self.assertEqual(response.status_code, 201, response.text)

    def test_failed_commit_without_retry_leaves_no_external_state(self):
        with patch.object(Session, "commit", side_effect=OperationalError("commit", {}, Exception("down"))), \
             patch.object(self.rt.features, "record_and_compute") as features, \
             patch.object(self.rt.graph, "add_event") as graph:
            self.assertEqual(self.post("failed").status_code, 503)
            features.assert_not_called()
            graph.assert_not_called()
        with session_scope() as session:
            for model in (RiskEvent, Decision, RiskCase, EventProjection):
                self.assertEqual(session.exec(select(model)).all(), [])
        response = self.post("different", 1, "u2").json()
        self.assertEqual(response["features"]["device_order_count_1h"], 1)
        self.assertEqual(response["features"]["related_account_count"], 0)

    def test_partial_projection_retries_without_resubmission(self):
        with patch.object(self.rt.graph, "add_event", side_effect=ConnectionError("down")):
            response = self.post("outbox")
        self.assertEqual(response.status_code, 200, response.text)
        with session_scope() as session:
            self.assertFalse(session.exec(select(EventProjection)).one().projected)
        # Simulate one background-worker cycle using a fresh session (also restart-safe).
        class OneCycle:
            calls = 0
            def wait(self, seconds):
                self.calls += 1
                return self.calls > 1
        projection_worker(self.rt, OneCycle())
        with session_scope() as session:
            self.assertTrue(session.exec(select(EventProjection)).one().projected)
            self.assertEqual(len(session.exec(select(Decision)).all()), 1)
            project_pending(self.rt, session)
        self.assertEqual(self.post("next", 1).json()["features"]["device_order_count_1h"], 2)

    def test_redis_outage_does_not_change_authoritative_features(self):
        with patch.object(self.rt.features, "record_and_compute", side_effect=ConnectionError("down")):
            self.assertEqual(self.post("first").status_code, 200)
            response = self.post("second", 1, "u2")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["features"]["device_order_count_1h"], 2)
        self.assertEqual(response.json()["features"]["related_account_count"], 1)

    def test_late_event_recomputes_with_original_rules_and_creates_case(self):
        self.rule()
        self.assertEqual(self.post("later", 20).json()["decision"], "PASS")
        self.client.put("/api/v1/rules/R-199", headers=self.auth, json={"score": 1})
        self.assertEqual(self.post("early", 10, "u2").status_code, 200)
        with session_scope() as session:
            later = session.exec(select(Decision).where(Decision.user_id == "u1")).one()
            self.assertEqual((later.score, later.decision), (90, "REJECT"))
            self.assertEqual(later.features["device_order_count_1h"], 2)
            self.assertEqual(later.features["related_account_count"], 1)
            self.assertEqual(len(session.exec(select(RiskCase)).all()), 1)
            decision_id = later.id
        trace = self.client.get(f"/api/v1/decisions/{decision_id}", headers=self.auth).json()
        self.assertEqual(trace["original"]["decision"], "PASS")
        self.assertEqual(len(trace["corrections"]), 1)
        self.assertTrue(self.post("early", 10, "u2").json()["duplicate"])
        self.assertTrue(self.client.get("/api/v1/audit/verify", headers=self.auth).json()["ok"])

    def test_arbitrarily_late_event_repairs_graph_without_polluting_old_window(self):
        self.post("later")
        response = self.post("very-old", -7200, "u2")
        self.assertEqual(response.status_code, 200, response.text)
        with session_scope() as session:
            later = session.exec(select(Decision).where(Decision.user_id == "u1")).one()
            self.assertEqual(later.features["device_order_count_1h"], 1)
            self.assertEqual(later.features["related_account_count"], 1)

    def test_recompute_failure_rolls_back_incoming_and_correction(self):
        self.rule()
        self.post("later", 20)
        with patch("app.services.reliability.repair_later", side_effect=OperationalError("repair", {}, Exception("down"))):
            self.assertEqual(self.post("early", 10).status_code, 503)
        with session_scope() as session:
            self.assertEqual(len(session.exec(select(RiskEvent)).all()), 1)
            self.assertEqual(session.exec(select(Decision)).one().decision, "PASS")
            self.assertEqual(session.exec(select(RiskCase)).all(), [])

    def test_same_second_repair_survives_database_timestamp_truncation(self):
        from datetime import datetime
        self.now = int(self.now)
        self.rule()
        self.post("later", 0.8)
        with session_scope() as session:
            for model in (RiskEvent, Decision):
                row = session.exec(select(model)).one()
                row.event_time = datetime.fromtimestamp(self.now)
                session.add(row)
        self.assertEqual(self.post("early", 0.2, "u2").status_code, 200)
        with session_scope() as session:
            later = session.exec(select(Decision).where(Decision.user_id == "u1")).one()
            early = session.exec(select(Decision).where(Decision.user_id == "u2")).one()
            self.assertEqual(later.features["device_order_count_1h"], 2)
            self.assertEqual(early.features["device_order_count_1h"], 1)

    def test_concurrent_duplicate_commits_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.post("same"), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(sorted(r.json()["duplicate"] for r in responses), [False, True])
        with session_scope() as session:
            self.assertEqual(len(session.exec(select(RiskEvent)).all()), 1)
            self.assertEqual(len(session.exec(select(EventProjection)).all()), 1)
