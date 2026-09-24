import json
import tempfile
import unittest
from pathlib import Path

import eval_model_selection as evaluation


class EvaluationTests(unittest.TestCase):
    def test_snapshots_cover_contrasts_with_holdout_and_no_best_model_labels(self):
        cases = evaluation.CASES
        self.assertGreaterEqual(len(cases), 12)
        categories = {c["category"] for c in cases}
        self.assertTrue({"small_risky", "large_mechanical", "debugging", "flaky_tests",
                         "security", "concurrency", "ambiguous", "blocked", "buried_evidence",
                         "long_context", "unavailable", "override", "retry"} <= categories)
        self.assertGreaterEqual(sum(c["split"] == "holdout" for c in cases), 4)
        self.assertTrue(all(c["label_kind"] == "diagnostic_hypothesis" and
                            "best_model" not in c for c in cases))
        self.assertTrue(any(c["id"] == "simple_text" and c["run_live"] for c in cases))

    def test_snapshot_verification_target_is_absent_and_acceptance_is_executable(self):
        case = next(c for c in evaluation.CASES if c["id"] == "small_risky_tenant")
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            evaluation.stage(case, work)
            self.assertFalse((work / case["verify_file"]).exists())
            self.assertFalse(evaluation.accept(case, work)["passed"])
            (work / "solution.py").write_text(
                "def allowed(user_tenant, resource_tenant):\n    return user_tenant == resource_tenant\n")
            self.assertTrue(evaluation.accept(case, work)["passed"])

    def test_acceptance_does_not_follow_solution_symlink(self):
        case = next(c for c in evaluation.CASES if c["id"] == "small_risky_tenant")
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            evaluation.stage(case, work)
            (work / "other.py").write_text(
                "def allowed(user_tenant, resource_tenant):\n    return user_tenant == resource_tenant\n")
            (work / "solution.py").symlink_to(work / "other.py")
            self.assertFalse(evaluation.accept(case, work)["passed"])

    def test_summary_never_counts_missing_or_failed_acceptance_as_success(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            path.joinpath("small_risky_tenant__sol.json").write_text(json.dumps({
                "case": "small_risky_tenant", "policy": "sol", "exit_code": 0,
                "acceptance": {"passed": False}, "controller": {
                    "status": "verified", "attempts": 1, "total_elapsed_seconds": 3.0,
                    "known_cost_usd": 0, "unknown_cost": True,
                    "trace": [{"kind": "sol_implementation", "tokens": {"input": 2, "output": 1}}]}}))
            summary = evaluation.summarize(path)
            self.assertEqual(summary["sol"]["observed"], 1)
            self.assertEqual(summary["sol"]["acceptance_passed"], 0)
            self.assertTrue(summary["sol"]["unknown_cost"])
            self.assertGreater(len(summary["unmeasured"]), 0)
            self.assertNotIn("winner", summary)


if __name__ == "__main__":
    unittest.main()
