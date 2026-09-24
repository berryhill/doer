"""Human-readable final report regressions (offline)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doer_loop import cli


class SummaryTests(unittest.TestCase):
    def test_report_renders_recorded_values_and_distinguishes_costs(self):
        payload = {
            "status": "verified", "attempts": 1,
            "message": "Model checks and independent verifier passed.",
            "completion": "The file passed its narrow check.", "completion_error": None,
            "trace": [
                {"kind": "gate", "attempt": None, "model": "laya", "provider": "local",
                 "elapsed_seconds": 0.125, "tokens": None, "usd": None,
                 "cost_status": "local_no_billed_api", "cost_source": "local",
                 "evidence": {"specified": True, "result_defined": True}, "error": None},
                {"kind": "sol_implementation", "attempt": 1, "model": "sol", "provider": "openai-codex",
                 "elapsed_seconds": 1.25, "tokens": {"input": 7, "output": 3}, "usd": 0,
                 "cost_status": "included", "cost_source": "subscription",
                 "evidence": "wrote file", "error": None},
                {"kind": "laya_judgment", "attempt": 1, "model": "laya", "provider": "local",
                 "elapsed_seconds": 0.5, "tokens": None, "usd": None,
                 "cost_status": "local_no_billed_api", "cost_source": "local",
                 "evidence": {"done": True, "stays_in_scope": True, "fulfills": True,
                              "works": True, "practices": True}, "error": None},
                {"kind": "independent_verifier", "attempt": 1, "model": "file_check", "provider": "local",
                 "elapsed_seconds": 0.01, "tokens": None, "usd": None,
                 "cost_status": "local_no_billed_api", "cost_source": "local",
                 "evidence": True, "error": None},
                {"kind": "luna_completion", "attempt": None, "model": "luna", "provider": "openai-codex",
                 "elapsed_seconds": 0.4, "tokens": {"input": 2, "output": 4}, "usd": None,
                 "cost_status": "unknown", "cost_source": None,
                 "evidence": "The file passed its narrow check.", "error": None},
            ],
            "total_elapsed_seconds": 2.5, "known_cost_usd": 0, "unknown_cost": True,
        }
        expected = "\n".join([
            "Outcome: verified; exit code: 0; attempts: 1",
            "Luna completion: The file passed its narrow check.",
            "Message: Model checks and independent verifier passed.",
            "Trace (execution order):",
            '1. gate; attempt: none; model: laya; provider: local; elapsed: 0.125s; tokens: unavailable; cost: local unpriced compute (no billed API charge); evidence: {"result_defined": true, "specified": true}',
            '2. sol_implementation; attempt: 1; model: sol; provider: openai-codex; elapsed: 1.25s; tokens: {"input": 7, "output": 3}; cost: subscription-included API charge ($0 recorded; source: subscription); evidence: "wrote file"',
            '3. laya_judgment; attempt: 1; model: laya; provider: local; elapsed: 0.5s; tokens: unavailable; cost: local unpriced compute (no billed API charge); evidence: {"done": true, "fulfills": true, "practices": true, "stays_in_scope": true, "works": true}',
            '4. independent_verifier; attempt: 1; model: file_check; provider: local; elapsed: 0.01s; tokens: unavailable; cost: local unpriced compute (no billed API charge); evidence: true',
            '5. luna_completion; attempt: none; model: luna; provider: openai-codex; elapsed: 0.4s; tokens: {"input": 2, "output": 4}; cost: unknown; evidence: "The file passed its narrow check."',
            "Total elapsed: 2.5s; known cost: $0; unknown cost: yes (total cost unknown)",
        ])
        self.assertEqual(cli.format_report(payload, 0), expected)
        self.assertEqual(cli.format_report(payload, 0), expected)

    def test_main_verified_report_is_inside_json_and_matches_trace(self):
        class Client:
            def ask(self, state, questions):
                return {key: True for key in questions}
            def route(self, task, candidates):
                return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}

        def fake_hermes(prompt, model, workspace, **kwargs):
            if "observed_control_trace" in prompt:
                return cli.HermesResponse("The artifact passed a narrow check.", "luna", {"input": 2})
            (Path(workspace) / "result.txt").write_text("ok")
            return cli.HermesResponse("wrote result.txt", "sol", {"input": 7, "output": 3}, "actual-sol")

        def cost(session_id, **kwargs):
            return ({"usd": 0, "cost_status": "included", "cost_source": "subscription"}
                    if session_id == "sol" else
                    {"usd": 0.12, "cost_status": "actual", "cost_source": "api"})

        with tempfile.TemporaryDirectory() as workspace, \
             patch("doer_loop.cli.make_client", return_value=Client()), \
             patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
             patch("doer_loop.cli.session_cost", side_effect=cost), \
             patch("sys.argv", ["doer", "create result.txt", "--workspace", workspace,
                                "--verify-file", "result.txt", "--execute"]), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr, \
             patch("builtins.print") as output:
            code = cli.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(code, 0)
        self.assertEqual(payload["report"], cli.format_report(payload, code))
        self.assertEqual(stderr.getvalue(), payload["report"] + "\n")
        self.assertEqual(len(payload["trace"]), 6)
        self.assertIn("actual-sol", payload["report"])
        self.assertIn("subscription-included API charge", payload["report"])
        self.assertIn("known cost: $0.12; unknown cost: no", payload["report"])

    def test_main_error_report_preserves_failure_and_completion_error(self):
        class Client:
            def ask(self, state, questions):
                return {key: True for key in questions}
            def route(self, task, candidates):
                return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}

        def fake_hermes(prompt, model, workspace, **kwargs):
            if "observed_control_trace" in prompt:
                raise RuntimeError("Luna offline")
            raise RuntimeError("Sol timed out")

        with tempfile.TemporaryDirectory() as workspace, \
             patch("doer_loop.cli.make_client", return_value=Client()), \
             patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
             patch("sys.argv", ["doer", "create result.txt", "--workspace", workspace,
                                "--verify-file", "result.txt", "--execute"]), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr, \
             patch("builtins.print") as output:
            code = cli.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(code, 1)
        self.assertEqual(payload["report"], cli.format_report(payload, code))
        self.assertEqual(stderr.getvalue(), payload["report"] + "\n")
        self.assertIn("Outcome: error; exit code: 1; attempts: 1", payload["report"])
        self.assertIn("Luna completion error: RuntimeError: Luna offline", payload["report"])
        self.assertIn("error: RuntimeError: Sol timed out", payload["report"])
        self.assertIn("cost: unknown", payload["report"])
        self.assertIn("unknown cost: yes (total cost unknown)", payload["report"])

    def test_incomplete_report_lists_all_attempts_and_failed_verdicts(self):
        class Client:
            def ask(self, state, questions):
                if "implementer_report" in state:
                    return {key: key != "works" for key in questions}
                return {key: True for key in questions}
            def route(self, task, candidates):
                return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}

        def fake_hermes(prompt, model, workspace, **kwargs):
            if "observed_control_trace" in prompt:
                return cli.HermesResponse("Checks did not pass.", "completion", {"output": 4})
            if "Failed checks:" in prompt:
                return cli.HermesResponse("Repair the missing artifact.", "diagnosis", {"output": 3})
            return cli.HermesResponse("reported completion", "implementation", {"output": 2})

        with tempfile.TemporaryDirectory() as workspace, \
             patch("doer_loop.cli.make_client", return_value=Client()), \
             patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
             patch("doer_loop.cli.session_cost", return_value={"usd": None, "cost_status": "unknown", "cost_source": None}), \
             patch("sys.argv", ["doer", "create result.txt", "--workspace", workspace,
                                "--verify-file", "result.txt", "--execute"]), \
             patch("builtins.print") as output:
            code = cli.main()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual((code, payload["status"], payload["attempts"]), (1, "incomplete", 3))
        self.assertEqual(payload["report"], cli.format_report(payload, code))
        self.assertIn("works, independent_verification", payload["report"])
        self.assertIn('"works": false', payload["report"])
        self.assertIn('"failure": "result.txt is missing"', payload["report"])
        self.assertIn('"passed": false', payload["report"])
        self.assertEqual(len(payload["trace"]), len(payload["report"].split("\n")) - 5)
        self.assertIn("Luna completion: Checks did not pass.", payload["report"])


if __name__ == "__main__":
    unittest.main()