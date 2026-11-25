import tests  # noqa: F401  必须第一行
import unittest

import fakeredis

from app.engine.features import FeatureEngine, LateEventError


def ev(etype, uid, ts, key, **kw):
    return {"event_type": etype, "user_id": uid, "ts": ts, "event_key": key, **kw}


class SlidingWindowTest(unittest.TestCase):
    def setUp(self):
        self.r = fakeredis.FakeRedis(decode_responses=True)
        self.fe = FeatureEngine(self.r, "t")

    def test_precise_sliding_not_fixed_window(self):
        t = 1_700_000_000
        # 跨整点：固定窗口会在整点清零，滑动窗口不会
        for i, dt in enumerate([0, 600, 1200, 1800, 2400]):
            f = self.fe.record_and_compute(ev("order_create", f"u{i}", t + dt, f"k{i}", device_id="D"))
        self.assertEqual(f["device_order_count_1h"], 5)
        f = self.fe.record_and_compute(ev("order_create", "u9", t + 3600, "k5", device_id="D"))
        self.assertEqual(f["device_order_count_1h"], 6)        # t+0 恰好在边界上，仍算入
        f = self.fe.record_and_compute(ev("order_create", "u9", t + 3601, "k6", device_id="D"))
        self.assertEqual(f["device_order_count_1h"], 6)        # t+0 滑出，t+3601 进入

    def test_same_event_key_not_double_counted(self):
        t = 1_700_000_000
        self.fe.record_and_compute(ev("coupon_receive", "u", t, "same", device_id="D"))
        f = self.fe.record_and_compute(ev("coupon_receive", "u", t, "same", device_id="D"))
        self.assertEqual(f["user_coupon_count_1h"], 1)

    def test_distinct_accounts(self):
        t = 1_700_000_000
        for i in range(4):
            f = self.fe.record_and_compute(ev("login", f"u{i % 2}", t + i, f"k{i}", ip="1.1.1.1"))
        self.assertEqual(f["ip_account_count_24h"], 2)
        f = self.fe.record_and_compute(ev("login", "u5", t + 86400 + 3, "k9", ip="1.1.1.1"))
        self.assertEqual(f["ip_account_count_24h"], 2)        # u0 最后出现于 t+2 → 已滑出；u1 在 t+3 恰在边界仍算；加上 u5 = 2

    def test_out_of_order_distinct_accounts(self):
        for ts in (10000, 10200, 10100):
            f = self.fe.record_and_compute(ev("login", "u", ts, str(ts), device_id="D"))
        self.assertEqual(f["device_account_count_24h"], 1)
        f = self.fe.record_and_compute(ev("login", "v", 10150, "v", device_id="D"))
        self.assertEqual(f["device_account_count_24h"], 2)

    def test_too_late_event_is_explicitly_rejected(self):
        self.fe.record_and_compute(ev("login", "u", 10000, "a", device_id="D"))
        with self.assertRaises(LateEventError):
            self.fe.record_and_compute(ev("login", "v", 6000, "b", device_id="D"))
        f = self.fe.record_and_compute(ev("login", "u", 10001, "c", device_id="D"))
        self.assertEqual(f["device_account_count_24h"], 1)

    def test_refund_rate_and_new_account(self):
        t = 1_700_000_000
        self.fe.record_and_compute(ev("order_create", "u", t, "o1", phone="P"))
        self.fe.record_and_compute(ev("order_create", "u", t + 1, "o2", phone="P"))
        f = self.fe.record_and_compute(ev("after_sale_apply", "u", t + 2, "a1", phone="P",
                                          payload={"register_ts": t - 86400 * 2, "refund_reason": "not_received"}))
        self.assertEqual(f["phone_refund_rate_24h"], 0.5)
        self.assertTrue(f["is_new_account"])
        self.assertEqual(f["refund_reason"], "not_received")

    def test_namespace_isolation(self):
        t = 1_700_000_000
        self.fe.record_and_compute(ev("order_create", "u", t, "k1", device_id="D"), namespace="sim:x")
        f = self.fe.record_and_compute(ev("order_create", "u", t + 1, "k2", device_id="D"))
        self.assertEqual(f["device_order_count_1h"], 1)        # 仿真数据不影响线上
        self.assertGreater(self.fe.drop_namespace("sim:x"), 0)
        self.assertFalse(list(self.r.scan_iter("t:sim:x:*")))


if __name__ == "__main__":
    unittest.main()
