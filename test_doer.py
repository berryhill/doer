import unittest
from doer import run, Gate, Verdict


class DoerTests(unittest.TestCase):
    def test_initial_contract_blocks_implementation(self):
        calls = []
        def implement(prompt):
            calls.append(prompt)
            return "work"
        result = run("make it better", lambda task: Gate(False, False), implement,
                     lambda task, work: Verdict(True, False, True, True),
                     lambda task, work, failed: "What result should I produce?")
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.message, "What result should I produce?")
        self.assertEqual(calls, [])

    def test_success_stops_on_first_verified_iteration(self):
        prompts = []
        def implement(prompt):
            prompts.append(prompt)
            return "verified artifact"
        result = run("write README", lambda task: Gate(True, True), implement,
                     lambda task, work: Verdict(True, False, True, True),
                     lambda task, work, failed: self.fail("Luna should not run"))
        self.assertEqual(result.status, "judged_pass")
        self.assertEqual(len(prompts), 1)

    def test_failed_checks_feed_luna_feedback_to_next_attempt(self):
        prompts, failures = [], []
        def implement(prompt):
            prompts.append(prompt)
            return "artifact"
        def judge(task, work):
            return Verdict(False, True, False, True) if len(prompts) == 1 else Verdict(True, False, True, True)
        def diagnose(task, work, failed):
            failures.append(failed)
            return "Fix missing output and scope drift."
        result = run("task", lambda task: Gate(True, True), implement, judge, diagnose)
        self.assertEqual(result.status, "judged_pass")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(failures, [("done", "drift", "fulfills")])
        self.assertIn("Fix missing output and scope drift.", prompts[1])

    def test_bad_practices_count_as_failed_check(self):
        self.assertEqual(Verdict(True, False, True, True, False).failures(), ("practices",))

    def test_independent_verifier_blocks_false_model_pass_and_repairs(self):
        calls = []
        def implement(prompt):
            calls.append(prompt)
            return "I finished it."
        def verify(task, work):
            return len(calls) > 1
        result = run("task", lambda task: Gate(True, True), implement,
                     lambda task, work: Verdict(True, False, True, True),
                     lambda task, work, failed: "Actual artifact missing", verify=verify)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.attempts, 2)
        self.assertIn("Actual artifact missing", calls[1])

    def test_three_attempt_cap_does_not_claim_success(self):
        calls = []
        def implement(prompt):
            calls.append(prompt)
            return "partial"
        result = run("task", lambda task: Gate(True, True), implement,
                     lambda task, work: Verdict(False, False, False, False),
                     lambda task, work, failed: "Needs more work")
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
