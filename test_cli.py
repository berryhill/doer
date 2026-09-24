import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from cli import verify_file


class AdapterTests(unittest.TestCase):
    def test_default_sol_is_not_reported_as_account_confirmed(self):
        import cli
        import io
        class Decisions:
            def ask(self, state, questions):
                return {key: True for key in questions}
        def fake_hermes(prompt, model, workspace, **kwargs):
            if "observed_control_trace" in prompt:
                return cli.HermesResponse("Limited check passed.", "luna", {"input": 1}, model)
            (Path(workspace) / "result.txt").write_text("done")
            return cli.HermesResponse("Wrote result.txt and checked it.", "implementation", {"input": 2}, model)
        with tempfile.TemporaryDirectory() as work, patch("cli.make_client", return_value=Decisions()), \
             patch("cli.hermes", side_effect=fake_hermes), \
             patch("cli.session_cost", return_value={"usd": 0, "cost_status": "included", "cost_source": None}), \
             patch("sys.argv", ["doer", "Create result.txt containing done", "--workspace", work,
                                "--verify-file", "result.txt", "--expect-text", "done", "--execute"]), \
             patch("sys.stderr", new_callable=io.StringIO), patch("builtins.print") as output:
            self.assertEqual(cli.main(), 0)
        import json
        result = json.loads(output.call_args.args[0])
        selection = next(s for s in result["trace"] if s["kind"] == "model_selection")
        self.assertEqual(selection["evidence"]["stages"][0]["source"], "default_unverified")

    def test_opt_in_routing_records_pipeline_and_actual_model(self):
        import cli
        import io
        class Decisions:
            backend = "laya"
            def ask(self, state, questions):
                return {key: True for key in questions}
            def choose_family(self, task, families):
                self.families = families
                return {"choice": "gpt-6-luna", "confidence": 0.9}
        decisions = Decisions()
        def fake_hermes(prompt, model, workspace, **kwargs):
            if "observed_control_trace" in prompt:
                return cli.HermesResponse("A limited file check passed.", "luna", {"input": 1}, model)
            (Path(workspace) / "result.txt").write_text("done")
            return cli.HermesResponse("Wrote and read result.txt.", "implementation",
                                      {"input": 2}, "gpt-6-luna-actual")
        with tempfile.TemporaryDirectory() as work, \
             patch("cli.make_client", return_value=decisions), \
             patch("cli.hermes", side_effect=fake_hermes), \
             patch("cli.session_cost", return_value={"usd": 0, "cost_status": "included", "cost_source": None}), \
             patch("sys.argv", ["doer", "Create result.txt containing done", "--workspace", work,
                                "--verify-file", "result.txt", "--expect-text", "done", "--execute",
                                "--routing-policy", "laya", "--available-model", "gpt-6-sol",
                                "--available-model", "gpt-6-luna"]), \
             patch("sys.stderr", new_callable=io.StringIO), patch("builtins.print") as output:
            code = cli.main()
        self.assertEqual(code, 0)
        import json
        result = json.loads(output.call_args.args[0])
        selection = next(s for s in result["trace"] if s["kind"] == "model_selection")
        self.assertEqual(decisions.families, ("gpt-6-sol", "gpt-6-luna"))
        self.assertEqual(selection["evidence"]["model"], "gpt-6-luna")
        self.assertEqual(selection["evidence"]["stages"][-2]["stage"], "family_choice")
        self.assertEqual(next(s for s in result["trace"] if s["kind"] == "sol_implementation")["model"],
                         "gpt-6-luna-actual")

    def test_executable_entrypoint_shows_help_without_server(self):
        entry = Path(__file__).parent / "doer"
        self.assertTrue(entry.is_file())
        self.assertTrue(entry.stat().st_mode & 0o111)
        import subprocess
        result = subprocess.run([str(entry), "--help"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--backend", result.stdout)

    def test_file_verifier_checks_content_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "result.txt"
            self.assertFalse(verify_file(root, "result.txt", "done"))
            path.write_text("done\n")
            self.assertTrue(verify_file(root, "result.txt", "done"))
            self.assertFalse(verify_file(root, "result.txt", "other"))
            self.assertFalse(verify_file(root, "../outside.txt", None))
            link = Path(root) / "alias.txt"
            link.symlink_to(path)
            self.assertFalse(verify_file(root, "alias.txt", "done"))

    def test_hermes_uses_ambient_configuration_by_default(self):
        from cli import hermes
        class Proc:
            returncode = 0
            stdout = json.dumps({"type": "system", "model": "ambient-model"}) + "\n" + json.dumps({
                "type": "result", "exit_code": 0, "text": "READY", "session_id": "sid-ambient",
                "tokens": {"input": 2, "output": 1}})
            stderr = ""
        with patch("cli.subprocess.run", return_value=Proc()) as invoke:
            response = hermes("test", None, "/work")
        self.assertEqual((response.text, response.model), ("READY", "ambient-model"))
        command = invoke.call_args.args[0]
        self.assertEqual(command, ["hermes", "chat", "--query-file", "-", "--format", "stream-json",
                                   "--source", "tool", "--in", "/work",
                                   "--max-turns", "20", "--run-budget", "300"])
        self.assertEqual(invoke.call_args.kwargs["input"], "test")

    def test_hermes_forwards_only_explicit_overrides(self):
        from cli import hermes
        class Proc:
            returncode = 0
            stdout = json.dumps({"type": "result", "exit_code": 0, "text": "READY",
                                 "session_id": "sid-override", "tokens": {"input": 2, "output": 1}})
            stderr = ""
        with patch("cli.subprocess.run", return_value=Proc()) as invoke:
            hermes("test", "custom-model", "/work", profile="custom", provider="custom-provider")
        command = invoke.call_args.args[0]
        self.assertEqual(command, ["hermes", "-p", "custom", "chat", "--query-file", "-", "--format", "stream-json",
                                   "-m", "custom-model", "--provider", "custom-provider",
                                   "--source", "tool", "--in", "/work",
                                   "--max-turns", "20", "--run-budget", "300"])

    def test_local_repair_probe_forces_exactly_one_failed_verification(self):
        from cli import local_repair_probe
        verify = local_repair_probe(lambda task, work: True)
        self.assertFalse(verify("task", "work"))
        self.assertTrue(verify("task", "work"))


if __name__ == "__main__": unittest.main()
