import tests  # noqa: F401  必须第一行
import unittest

from app.engine.dsl import CompiledRule, DSLError, counterfactual, evaluate, parse, validate
from app.engine.whatif import decision_whatif
from app.engine.features import FEATURES


class DSLTest(unittest.TestCase):
    def test_basic_and(self):
        ast = parse("device_order_count_1h > 5 AND is_new_account == true")
        self.assertTrue(evaluate(ast, {"device_order_count_1h": 6, "is_new_account": True}))
        self.assertFalse(evaluate(ast, {"device_order_count_1h": 5, "is_new_account": True}))

    def test_precedence_and_over_or(self):
        ast = parse("a == 1 OR b == 1 AND c == 1")          # = a==1 OR (b==1 AND c==1)
        self.assertTrue(evaluate(ast, {"a": 1, "b": 0, "c": 0}))
        self.assertFalse(evaluate(ast, {"a": 0, "b": 1, "c": 0}))
        ast2 = parse("(a == 1 OR b == 1) AND c == 1")
        self.assertFalse(evaluate(ast2, {"a": 1, "b": 0, "c": 0}))

    def test_not_in_strings_case_insensitive_keywords(self):
        ast = parse("event_type in ['order_create', \"order_pay\"] and not refund_reason == 'quality'")
        self.assertTrue(evaluate(ast, {"event_type": "order_pay", "refund_reason": None}))
        self.assertFalse(evaluate(ast, {"event_type": "login"}))
        ast2 = parse("event_type NOT IN ['login']")
        self.assertTrue(evaluate(ast2, {"event_type": "order_pay"}))

    def test_missing_feature_is_false_not_error(self):
        self.assertFalse(evaluate(parse("x > 5"), {}))

    def test_float_and_negative(self):
        self.assertTrue(evaluate(parse("rate >= 0.6 AND d > -1"), {"rate": 0.6, "d": 0}))

    def test_trace_records_every_condition(self):
        rule = CompiledRule("R", "r", "a > 1 AND b == 'x'", 10)
        ok, trace = rule.run({"a": 3, "b": "y"})
        self.assertFalse(ok)
        self.assertEqual([t["condition"] for t in trace], ["a > 1", "b == 'x'"])
        self.assertEqual([t["matched"] for t in trace], [True, False])
        self.assertEqual(trace[0]["actual"], 3)

    def test_syntax_errors_have_position(self):
        for bad in ["", "a >", "a > 1 AND", "(a > 1", "a > 1)", "a $ 1", "a IN [b]"]:
            with self.assertRaises(DSLError, msg=bad):
                parse(bad)
        with self.assertRaises(DSLError) as cm:
            parse("a > 1 AND")
        self.assertEqual(cm.exception.pos, 9)

    def test_no_code_execution(self):
        with self.assertRaises(DSLError):
            parse("__import__('os').system('dir')")
        with self.assertRaises(DSLError):
            parse("a.b > 1")

    def test_unknown_field_rejected(self):
        with self.assertRaises(DSLError):
            validate("not_a_feature > 1", set(FEATURES))
        validate("device_order_count_1h > 5", set(FEATURES))

    def test_rule_scope(self):
        r = CompiledRule("R", "r", "amount > 1", 10, ["order_create"])
        self.assertTrue(r.applies_to("order_create"))
        self.assertFalse(r.applies_to("login"))
        self.assertTrue(CompiledRule("R", "r", "amount > 1", 10).applies_to("login"))


class CounterfactualTest(unittest.TestCase):
    def test_and_rule_all_conditions_critical(self):
        cf = counterfactual(parse("device_order_count_1h >= 5 AND amount > 1000"),
                            {"device_order_count_1h": 6, "amount": 3999})
        self.assertTrue(cf["matched"])
        self.assertEqual(len(cf["critical"]), 2)
        # 6 离 5 的相对距离比 3999 离 1000 近，最小改动应落在设备下单数上
        self.assertEqual(cf["minimal_change"]["field"], "device_order_count_1h")
        self.assertIn("不再命中", cf["minimal_change"]["text"])

    def test_or_rule_with_both_branches_has_no_critical(self):
        cf = counterfactual(parse("a > 1 OR b > 1"), {"a": 5, "b": 5})
        self.assertTrue(cf["matched"])
        self.assertEqual(cf["critical"], [])
        self.assertIsNone(cf["minimal_change"])

    def test_unmatched_rule_reports_what_would_trigger(self):
        cf = counterfactual(parse("a >= 10 AND b == true"), {"a": 3, "b": True})
        self.assertFalse(cf["matched"])
        self.assertEqual(len(cf["critical"]), 1)
        self.assertIn("就会命中", cf["minimal_change"]["text"])


class DecisionWhatIfTest(unittest.TestCase):
    def _d(self, hits, score, decision, by="rules", feats=None):
        return {"decided_by": by, "decision": decision, "score": score, "hits": hits, "features": feats or {}}

    def _hit(self, code, expr, score):
        return {"code": code, "name": code, "score": score, "expression": expr, "conditions": [{"cid": 0}]}

    def test_pivotal_rule_and_margin(self):
        feats = {"a": 6, "b": 2}
        d = self._d([self._hit("R1", "a >= 5", 40), self._hit("R2", "b >= 2", 30)], 70, "REVIEW", feats=feats)
        w = decision_whatif(d, 60, 80)
        self.assertTrue(w["applicable"])
        self.assertEqual(w["pivotal_rules"], ["R1", "R2"])          # 去掉任一条都 < 60
        self.assertEqual(w["margin"]["down"], {"need": 11, "to": "PASS"})
        self.assertEqual(w["margin"]["up"], {"need": 10, "to": "REJECT"})
        self.assertEqual(w["rules"][0]["without"], {"score": 30, "decision": "PASS", "changes": True})
        self.assertTrue(w["rules"][0]["matched"])

    def test_stacked_rules_no_single_pivot(self):
        hits = [self._hit(f"R{i}", "a >= 1", 30) for i in range(4)]        # 120 截断到 100
        w = decision_whatif(self._d(hits, 100, "REJECT", feats={"a": 1}), 60, 80)
        self.assertEqual(w["pivotal_rules"], [])
        self.assertIn("叠加", w["summary"])
        self.assertIsNone(w["margin"]["up"])

    def test_greylist_pseudo_rule_and_list_decisions(self):
        grey = {"code": "GREYLIST", "name": "灰名单", "score": 20, "expression": "in_greylist == true", "conditions": []}
        w = decision_whatif(self._d([self._hit("R1", "a >= 1", 50), grey], 70, "REVIEW", feats={"a": 1}), 60, 80)
        self.assertIn("note", w["rules"][1])
        self.assertTrue(w["rules"][1]["without"]["changes"])
        self.assertFalse(decision_whatif(self._d([], 100, "REJECT", by="blacklist"), 60, 80)["applicable"])


if __name__ == "__main__":
    unittest.main()