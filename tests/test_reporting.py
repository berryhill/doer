"""Lifecycle completion and accounting regressions (offline)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doer_loop.controller import Gate, Verdict, run
from doer_loop import cli


class ReportingTests(unittest.TestCase):
    def test_completion_sentence_for_every_terminal_status(self):
        for task, gate, verdict, status in (
            ("", Gate(True, True), Verdict(True, False, True, True), "needs_input"),
            ("task", Gate(False, True), Verdict(True, False, True, True), "needs_input"),
            ("task", Gate(True, True), Verdict(True, False, True, True), "verified"),
            ("task", Gate(True, True), Verdict(False, False, False, False), "incomplete"),
        ):
            with self.subTest(status=status, task=task, gate=gate):
                seen = []
                def completion(outcome):
                    seen.append(outcome.status)
                    return f"The outcome is {outcome.status}, subject to the recorded checks."
                result = run(task, lambda _: gate, lambda _: "report", lambda *_: verdict,
                             lambda *_: "repair", verify=lambda *_: True, complete=completion)
                self.assertEqual(result.status, status)
                self.assertEqual(seen, [status])
                self.assertEqual(result.completion,
                                 f"The outcome is {status}, subject to the recorded checks.")
                self.assertIsNone(result.completion_error)

    def test_completion_once_for_all_outcomes_and_preserves_status_on_error(self):
        cases = [("", "needs_input", 0), ("task", "verified", 1),
                 ("task", "incomplete", 3)]
        for task, status, attempts in cases:
            with self.subTest(status=status):
                seen = []
                def completion(result):
                    seen.append((result.status, result.attempts, result.message, result.work))
                    raise RuntimeError("Luna unavailable")
                verdict = Verdict(status == "verified", False, status == "verified", status == "verified")
                result = run(task, lambda _: Gate(True, True), lambda _: "observed work",
                             lambda *_: verdict, lambda *_: "repair", verify=lambda *_: True,
                             complete=completion)
                self.assertEqual((result.status, result.attempts), (status, attempts))
                self.assertEqual(len(seen), 1)
                self.assertIn("Luna unavailable", result.completion_error)
                self.assertIsNone(result.completion)

    def test_completion_receives_failed_verdict_and_verification_evidence(self):
        seen = []
        result = run("task", lambda _: Gate(True, True), lambda _: "report",
                     lambda *_: Verdict(False, True, False, False),
                     lambda *_: "repair", verify=lambda *_: False,
                     complete=lambda outcome: seen.append(outcome) or "Low confidence: checks failed.")
        self.assertEqual(result.completion, "Low confidence: checks failed.")
        self.assertEqual(len(seen), 1)
        self.assertIn("independent_verification", seen[0].message)

    def test_stream_result_is_required_and_tokens_come_from_result(self):
        events = '\n'.join([json.dumps({"type": "text", "text": "not final"}),
                            json.dumps({"type": "result", "session_id": "sid-1",
                                        "exit_code": 0, "text": "done", "tokens": {
                                            "input": 7, "output": 3, "total": 10,
                                            "cache_read": 2, "cache_write": 1}})])
        class Proc:
            returncode = 0
            stdout = events
            stderr = ""
        with patch("doer_loop.cli.subprocess.run", return_value=Proc()) as invoke:
            response = cli.hermes("prompt", "gpt-6-luna", "/workspace")
        self.assertIn("stream-json", invoke.call_args.args[0])
        self.assertEqual(response.text, "done")
        self.assertEqual(response.session_id, "sid-1")
        self.assertEqual(response.tokens["input"], 7)
        with patch("doer_loop.cli.subprocess.run", return_value=type("P", (), {"returncode": 0, "stdout": '{}', "stderr": ""})()):
            with self.assertRaises(RuntimeError):
                cli.hermes("prompt", "gpt-6-luna", "/workspace")

    def test_accounting_reads_only_matching_session_and_rejects_unknown_cost(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, "
                             "estimated_cost_usd REAL, actual_cost_usd REAL, "
                             "cost_status TEXT, cost_source TEXT)")
                conn.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?)", [
                    ("sid-1", "sol", "", 0.04, 0, "estimated", "pricing"),
                    ("sid-1", "sol", "title_generation", 0, 0, None, None),
                    ("other", "sol", "", 40, 0, "actual", "api"),
                    ("null-price", "sol", "", None, None, "actual", "api")])
            charge = cli.session_cost("sid-1", db)
            self.assertEqual(charge["usd"], 0.04)
            self.assertEqual(charge["cost_status"], "estimated")
            self.assertEqual(charge["cost_source"], "pricing")
            self.assertIsNone(cli.session_cost("missing", db)["usd"])
            self.assertIsNone(cli.session_cost("null-price", db)["usd"])

    def test_controller_trace_has_order_and_failure_keeps_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            class Client:
                backend = "laya"
                def ask(self, state, questions):
                    return {key: True for key in questions}
                def route(self, task, candidates):
                    return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}
            calls = []
            def fake_hermes(prompt, model, workspace, **kwargs):
                calls.append(model)
                if "observed_control_trace" in prompt:
                    self.assertIn("uncalibrated", prompt)
                    raise RuntimeError("completion offline")
                (Path(workspace) / "result.txt").write_text("ok")
                return cli.HermesResponse("observed success", "sid-sol", {"input": 4, "output": 2})
            with patch("doer_loop.cli.make_client", return_value=Client()), patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
                 patch("doer_loop.cli.session_cost", return_value={"usd": None, "cost_status": "unknown", "cost_source": None}), \
                 patch("sys.argv", ["doer", "make result", "--workspace", d, "--verify-file", "result.txt", "--execute"]), \
                 patch("builtins.print") as output:
                code = cli.main()
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "verified")
            self.assertIn("completion offline", payload["completion_error"])
            self.assertEqual(calls, ["gpt-6-sol", "gpt-6-luna"])
            self.assertEqual([step["kind"] for step in payload["trace"]],
                             ["gate", "model_selection", "sol_implementation", "laya_judgment", "independent_verifier", "luna_completion"])
            self.assertTrue(all(step["elapsed_seconds"] >= 0 for step in payload["trace"]))
            self.assertEqual(payload["trace"][2]["tokens"]["input"], 4)
            self.assertTrue(payload["unknown_cost"])
            self.assertEqual(payload["known_cost_usd"], 0)
            self.assertGreaterEqual(payload["total_elapsed_seconds"], 0)

    def test_completion_accepts_single_sentence_with_filename_period(self):
        with tempfile.TemporaryDirectory() as d:
            class Client:
                def ask(self, state, questions):
                    return {key: True for key in questions}
                def route(self, task, candidates):
                    return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}
            sentence = "The result.txt artifact passed its narrow file check, but quality remains uncertain."
            def fake_hermes(prompt, model, workspace, **kwargs):
                if "observed_control_trace" in prompt:
                    return cli.HermesResponse(sentence, "sid-luna", {"input": 1, "output": 1})
                (Path(workspace) / "result.txt").write_text("hello")
                return cli.HermesResponse("created result.txt", "sid-sol", {"input": 1, "output": 1})
            with patch("doer_loop.cli.make_client", return_value=Client()), patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
                 patch("doer_loop.cli.session_cost", return_value={"usd": 0, "cost_status": "included", "cost_source": "none"}), \
                 patch("sys.argv", ["doer", "create result.txt", "--workspace", d, "--verify-file", "result.txt", "--expect-text", "hello", "--execute"]), \
                 patch("builtins.print") as output:
                code = cli.main()
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(code, 0)
            self.assertEqual(payload["completion"], sentence)
            self.assertIsNone(payload["completion_error"])

    def test_incomplete_keeps_nonzero_exit_when_completion_fails(self):
        with tempfile.TemporaryDirectory() as d:
            class Client:
                def ask(self, state, questions):
                    return {key: True for key in questions}
                def route(self, task, candidates):
                    return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}
            def fake_hermes(prompt, model, workspace, **kwargs):
                if "observed_control_trace" in prompt:
                    raise RuntimeError("completion offline")
                return cli.HermesResponse("reported success", "sid", {"input": 1, "output": 1})
            with patch("doer_loop.cli.make_client", return_value=Client()), patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
                 patch("doer_loop.cli.session_cost", return_value={"usd": None, "cost_status": "unknown", "cost_source": None}), \
                 patch("sys.argv", ["doer", "make result", "--workspace", d, "--verify-file", "result.txt", "--execute"]), \
                 patch("builtins.print") as output:
                code = cli.main()
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(code, 1)
            self.assertEqual((payload["status"], payload["attempts"]), ("incomplete", 3))
            self.assertIn("independent_verification", payload["message"])
            self.assertIn("completion offline", payload["completion_error"])
            self.assertEqual(payload["trace"][-1]["kind"], "luna_completion")

    def test_implementation_failure_reports_trace_and_luna_completion(self):
        with tempfile.TemporaryDirectory() as d:
            class Client:
                def ask(self, state, questions):
                    return {key: True for key in questions}
                def route(self, task, candidates):
                    return {"model": "gpt-6-sol", "choice": "gpt-6-sol", "confidence": 0.9, "reason": "selected"}
            def fake_hermes(prompt, model, workspace, **kwargs):
                if "One task in workspace" in prompt:
                    raise RuntimeError("Sol timed out")
                return cli.HermesResponse("Low confidence: implementation failed.",
                                          "sid-luna", {"input": 1, "output": 5})
            with patch("doer_loop.cli.make_client", return_value=Client()), patch("doer_loop.cli.hermes", side_effect=fake_hermes), \
                 patch("doer_loop.cli.session_cost", return_value={"usd": 0, "cost_status": "included", "cost_source": "none"}), \
                 patch("sys.argv", ["doer", "make result", "--workspace", d, "--verify-file", "result.txt", "--execute"]), \
                 patch("builtins.print") as output:
                code = cli.main()
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(code, 1)
            self.assertEqual((payload["status"], payload["attempts"]), ("error", 1))
            self.assertIn("Sol timed out", payload["message"])
            self.assertEqual(payload["completion"], "Low confidence: implementation failed.")
            self.assertEqual([step["kind"] for step in payload["trace"]],
                             ["gate", "model_selection", "sol_implementation", "luna_completion"])
            self.assertIn("Sol timed out", payload["trace"][2]["error"])


if __name__ == "__main__":
    unittest.main()