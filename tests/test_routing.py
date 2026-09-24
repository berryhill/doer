import unittest

from doer_loop.routing import choose_model, CANDIDATES


class RoutingTests(unittest.TestCase):
    def test_override_skips_availability_context_and_laya(self):
        decision = choose_model("task", (), 100, lambda *args: self.fail("called"),
                                override="gpt-6-sol-900k", policy="laya")
        self.assertEqual(decision["model"], "gpt-6-sol-900k")
        self.assertEqual([s["stage"] for s in decision["stages"]], ["override"])

    def test_unconfirmed_candidates_cannot_be_selected(self):
        seen = []
        def choose(task, families):
            seen.append(families)
            return {"choice": "gpt-6-astra", "confidence": 0.99}
        decision = choose_model("risky edit", ("gpt-6-sol", "gpt-6-luna"), 100, choose,
                                policy="laya")
        self.assertEqual(seen, [("gpt-6-sol", "gpt-6-luna")])
        self.assertEqual(decision["model"], "gpt-6-sol")
        self.assertEqual(decision["reason"], "unavailable_choice")
        self.assertEqual(decision["stages"][0]["eligible_families"],
                         ["gpt-6-sol", "gpt-6-luna"])
        self.assertEqual(decision["stages"][2]["context_bytes"], 100)

    def test_ambiguous_choice_falls_back_without_claiming_calibration(self):
        decision = choose_model("task", CANDIDATES, 80,
                                lambda *_: {"choice": "gpt-6-luna", "confidence": 0.31},
                                policy="laya")
        self.assertEqual(decision["model"], "gpt-6-sol")
        self.assertEqual(decision["reason"], "uncertain_family")
        self.assertEqual(decision["stages"][-2]["choice"], "gpt-6-luna")
        self.assertEqual(decision["stages"][-2]["confidence_kind"], "uncalibrated")

    def test_jev_threshold_can_remain_stricter_than_laya(self):
        decision = choose_model("task", CANDIDATES, 80,
                                lambda *_: {"choice": "gpt-6-luna", "confidence": 0.7},
                                policy="laya", min_confidence=0.8)
        self.assertEqual(decision["model"], "gpt-6-sol")

    def test_context_variant_requires_observed_token_count_and_alias_availability(self):
        choose = lambda *_: {"choice": "gpt-6-astra", "confidence": 0.95}
        with self.assertRaisesRegex(ValueError, "context fit unknown"):
            choose_model("task", CANDIDATES, 310000, choose, policy="laya")
        long = choose_model("task", CANDIDATES, 310000, choose, policy="laya",
                            observed_input_tokens=300000)
        self.assertEqual(long["model"], "gpt-6-astra-900k")
        self.assertEqual(long["stages"][2]["fit"], "long_context_indicated")
        with self.assertRaisesRegex(ValueError, "long-context"):
            choose_model("task", ("gpt-6-astra", "gpt-6-sol"), 310000, choose,
                         policy="laya", observed_input_tokens=300000)

    def test_default_and_rules_policy_do_not_call_laya(self):
        choose = lambda *_: self.fail("Laya used")
        default = choose_model("small but risky security fix", CANDIDATES, 29, choose)
        self.assertEqual(default["model"], "gpt-6-sol")
        self.assertEqual(default["reason"], "stable_default")
        rules = choose_model("Create x.txt containing hi", CANDIDATES, 26, choose,
                             policy="rules")
        self.assertIn(rules["model"], CANDIDATES)
        self.assertNotEqual(rules["reason"], "stable_default")

    def test_rules_baseline_distinguishes_simple_creation_from_risky_edit(self):
        choose = lambda *_: self.fail("Laya used")
        simple = choose_model("Create result.txt containing hello", CANDIDATES, 600,
                              choose, policy="rules")
        risky = choose_model("Create solution.py with a security check for tenant access", CANDIDATES,
                             600, choose, policy="rules")
        self.assertEqual(simple["model"], "gpt-6-luna")
        self.assertEqual(risky["model"], "gpt-6-sol")

    def test_fails_closed_when_no_confirmed_normal_candidate(self):
        with self.assertRaisesRegex(ValueError, "confirmed"):
            choose_model("task", (), 20, lambda *_: None, policy="laya")
        with self.assertRaisesRegex(ValueError, "unapproved"):
            choose_model("task", ("other-family",), 20, lambda *_: None, policy="laya")


if __name__ == "__main__":
    unittest.main()
