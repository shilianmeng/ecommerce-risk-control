import tests  # noqa: F401  必须第一行
import unittest

from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401
from app.engine import audit
from app.engine.graph import MemoryGraphStore
from app.models import AuditLog


class GraphTest(unittest.TestCase):
    def test_related_and_gangs(self):
        g = MemoryGraphStore()
        for i in range(4):                                    # 4 个号共用一台设备
            g.add_event({"user_id": f"w{i}", "device_id": "D1", "ip": f"ip{i}"})
        g.add_event({"user_id": "w4", "ip": "ip3"})           # 通过 IP 间接关联 → 2 跳
        g.add_event({"user_id": "x", "device_id": "D9"})      # 无关账号
        self.assertEqual(g.related_accounts("w0", hops=1), ["w1", "w2", "w3"])
        self.assertEqual(g.related_accounts("w0", hops=2), ["w1", "w2", "w3", "w4"])
        gangs = g.gangs(min_size=3)
        self.assertEqual(len(gangs), 1)
        self.assertEqual(gangs[0]["size"], 5)
        self.assertEqual(gangs[0]["shared_entities"][0], {"type": "Device", "value": "D1", "degree": 4})
        nb = g.neighborhood("w0", hops=1)
        self.assertTrue(any(n["center"] for n in nb["nodes"]))
        self.assertEqual(g.related_accounts("nobody"), [])


class AuditChainTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def test_chain_and_tamper_detection(self):
        with Session(self.engine) as s:
            for i in range(5):
                audit.append(s, "u", "act", "case", f"C{i}", {"n": i, "中文": "是"})
            s.commit()
            self.assertEqual(audit.verify(s), {"ok": True, "checked": 5, "broken_at": None, "reason": ""})

            row = s.get(AuditLog, 3)
            row.detail = {"n": 999, "中文": "是"}                # 篡改内容
            s.add(row)
            s.commit()
            res = audit.verify(s)
            self.assertFalse(res["ok"])
            self.assertEqual(res["broken_at"], 3)

    def test_deletion_detected(self):
        with Session(self.engine) as s:
            for i in range(3):
                audit.append(s, "u", "act", "case", f"C{i}", {})
            s.commit()
            s.delete(s.get(AuditLog, 2))
            s.commit()
            res = audit.verify(s)
            self.assertFalse(res["ok"])
            self.assertEqual(res["broken_at"], 3)


if __name__ == "__main__":
    unittest.main()