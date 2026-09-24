"""Exercise the controller with direct Laya and subprocess Hermes (no server)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

FAKE_LAYA = '''import os
from pathlib import Path
class Router:
    def __init__(self):
        with Path(os.environ["FAKE_LAYA_LOG"]).open("a") as f: f.write("load\\n")
    def predict(self, state, questions):
        with Path(os.environ["FAKE_LAYA_LOG"]).open("a") as f: f.write("predict " + ",".join(questions) + "\\n")
        if "implementation_family" in questions:
            return {"answers": {"implementation_family": {"choice": "gpt-6-sol", "answer_confidence": 0.99}}}
        if "works" in questions and os.environ.get("FAKE_BAD_JUDGMENT") == "1":
            count = sum("works" in line for line in
                        Path(os.environ["FAKE_LAYA_LOG"]).read_text().splitlines()
                        if line.startswith("predict "))
            if count == 1:
                return {"answers": {key: {"choice": "no" if key == "works" else "yes",
                                          "answer_confidence": 0.99} for key in questions}}
        return {"answers": {key: {"choice": "yes", "answer_confidence": 0.99}
                            for key in questions}}
'''

FAKE_HERMES = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert "chat" in args and args[args.index("--query-file") + 1] == "-"
assert args[args.index("--format") + 1] == "stream-json"
assert "--source" in args and args[args.index("--source") + 1] == "tool"
assert args[args.index("--max-turns") + 1] == "20"
assert args[args.index("--run-budget") + 1] == "300"
prompt = sys.stdin.read()
role = "luna" if prompt.startswith("Original task:") or "observed_control_trace" in prompt else "sol"
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as f: f.write(json.dumps({"args": args, "role": role, "prompt": prompt}) + "\\n")
if role == "luna":
    text = ("The artifact passed a limited file check with moderate confidence."
            if "observed_control_trace" in prompt else
            "Artifact missing; create result.txt with the expected content.")
else:
    count = sum(json.loads(line)["role"] == "sol" for line in log.read_text().splitlines())
    if count == 2 or os.environ.get("FAKE_FIRST_FILE") == "1":
        (Path(args[args.index("--in") + 1]) / "result.txt").write_text("hello\\n")
    text = "Implemented; test reports success."
model = args[args.index("-m") + 1] if "-m" in args else "ambient-model"
print(json.dumps({"type": "system", "model": model}))
print(json.dumps({"type": "result", "session_id": "fake-" + str(len(log.read_text().splitlines())),
                  "exit_code": 0, "text": text,
                  "tokens": {"input": 5, "output": 2, "total": 7, "cache_read": 0, "cache_write": 0}}))
'''


class IntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("DOER_LIVE_SMOKE") == "1", "opt-in real Laya/Sol smoke")
    def test_real_laya_and_sol_create_verified_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            completed = subprocess.run([str(ROOT / "doer"),
                                        "Create result.txt containing exactly hello in the workspace. No other files.",
                                        "--workspace", str(work), "--verify-file", "result.txt",
                                        "--expect-text", "hello", "--execute"],
                                       capture_output=True, text=True, timeout=420)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual(json.loads(completed.stdout)["status"], "verified")
            self.assertEqual((work / "result.txt").read_text().strip(), "hello")

    def run_fake(self, overrides=(), *, first_file=False, bad_judgment=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir, work = root / "bin", root / "work"
            bin_dir.mkdir()
            work.mkdir()
            (bin_dir / "laya.py").write_text(FAKE_LAYA)
            fake = bin_dir / "hermes"
            fake.write_text(FAKE_HERMES)
            fake.chmod(0o755)
            log, laya_log = root / "invocations.log", root / "laya.log"
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(bin_dir),
                   "FAKE_LOG": str(log), "FAKE_LAYA_LOG": str(laya_log),
                   "FAKE_FIRST_FILE": "1" if first_file else "0",
                   "FAKE_BAD_JUDGMENT": "1" if bad_judgment else "0"}
            completed = subprocess.run([sys.executable, "-m", "doer_loop.cli",
                                        "Create result.txt containing hello", "--workspace", str(work),
                                        "--verify-file", "result.txt", "--expect-text", "hello", "--execute",
                                        *overrides],
                                       capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertEqual((result["status"], result["attempts"]), ("verified", 2))
            self.assertEqual((work / "result.txt").read_text(), "hello\n")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([call["role"] for call in calls], ["sol", "luna", "sol", "luna"])
            self.assertIn("Artifact missing", calls[-2]["prompt"])
            self.assertIn("moderate confidence", result["completion"])
            self.assertEqual([step["kind"] for step in result["trace"]],
                             ["gate", "model_selection", "sol_implementation", "laya_judgment", "independent_verifier",
                              "luna_diagnosis", "sol_implementation", "laya_judgment",
                              "independent_verifier", "luna_completion"])
            for call in calls:
                args = call["args"]
                self.assertEqual(args[args.index("--in") + 1], str(work))
                self.assertNotIn("--yolo", args)
            self.assertEqual(laya_log.read_text().splitlines()[0], "load")
            self.assertEqual(len(laya_log.read_text().splitlines()),
                             5 if "laya" in overrides else 4)
            self.assertEqual(result["trace"][1]["evidence"]["reason"],
                             "explicit_override" if "--sol" in overrides else
                             "laya_choice" if "laya" in overrides else "stable_default")
            self.assertEqual([s["model"] for s in result["trace"] if s["kind"] == "sol_implementation"],
                             [overrides[overrides.index("--sol") + 1] if "--sol" in overrides else "gpt-6-sol"] * 2)
            self.last_result = result
            return calls

    def test_reported_success_does_not_pass_until_actual_artifact_exists(self):
        calls = self.run_fake()
        for call in calls:
            self.assertEqual(call["args"][:5], ["chat", "--query-file", "-", "--format", "stream-json"])
            self.assertNotIn("-p", call["args"])
            self.assertNotIn("--profile", call["args"])
            self.assertNotIn("--provider", call["args"])
            if call["role"] == "luna":
                self.assertEqual(call["args"][call["args"].index("-m") + 1], "gpt-6-luna")
            else:
                self.assertEqual(call["args"][call["args"].index("-m") + 1], "gpt-6-sol")

    def test_retry_prompt_reports_positive_judgment_but_failed_file_check(self):
        calls = self.run_fake()
        prompt = calls[2]["prompt"]
        self.assertIn("Original task: Create result.txt containing hello", prompt)
        self.assertIn("Required artifact: result.txt; expected exact text: 'hello'", prompt)
        self.assertIn("Observed result: result.txt is missing", prompt)
        self.assertIn("Laya judgment: passed all reported checks", prompt)
        self.assertIn("independent file check: failed (result.txt is missing)", prompt)
        self.assertIn("Keep: No independently verified working behavior", prompt)
        self.assertIn("Next attempt: Repair only these failures. Inspect the current workspace first", prompt)
        self.assertIn("Run the relevant checks and report their real output", prompt)
        self.assertIn("Do not invent evidence or change unrelated files", prompt)
        first_check = next(s for s in self.last_result["trace"] if s["kind"] == "independent_verifier")
        self.assertEqual(first_check["evidence"], {"passed": False,
                                                     "observed": "result.txt is missing",
                                                     "failure": "result.txt is missing"})

    def test_retry_prompt_preserves_verified_file_when_judgment_fails(self):
        calls = self.run_fake(first_file=True, bad_judgment=True)
        prompt = calls[2]["prompt"]
        self.assertIn("Laya judgment: failed works", prompt)
        self.assertIn("independent file check: passed", prompt)
        self.assertIn("Keep: Independent file check passed: result.txt exists", prompt)
        self.assertIn("'hello\\n'", prompt)

    def test_opt_in_family_selection_runs_once_before_retries(self):
        calls = self.run_fake(("--routing-policy", "laya", "--available-model", "gpt-6-sol",
                               "--available-model", "gpt-6-luna"))
        self.assertEqual([call["args"][call["args"].index("-m") + 1] for call in calls],
                         ["gpt-6-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-luna"])

    def test_luna_role_uses_luna_model_without_explicit_override(self):
        calls = self.run_fake()
        self.assertEqual([call["args"][call["args"].index("-m") + 1]
                          if "-m" in call["args"] else None for call in calls],
                         ["gpt-6-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-luna"])

    def test_explicit_overrides_reach_both_separate_chats(self):
        calls = self.run_fake(("--profile", "alt", "--provider", "other",
                               "--sol", "sol-model", "--luna", "luna-model"))
        for call in calls:
            args = call["args"]
            self.assertEqual(args[:4], ["-p", "alt", "chat", "--query-file"])
            self.assertEqual(args[args.index("--provider") + 1], "other")
            self.assertEqual(args[args.index("-m") + 1],
                             "luna-model" if call["role"] == "luna" else "sol-model")

    def test_independent_partial_overrides(self):
        for overrides, option, value in (("--profile", "-p", "alt"),
                                         ("--provider", "--provider", "other"),
                                         ("--sol", "-m", "sol-model"),
                                         ("--luna", "-m", "luna-model")):
            with self.subTest(overrides=overrides):
                calls = self.run_fake((overrides, value))
                for call in calls:
                    args = call["args"]
                    if overrides in ("--profile", "--provider"):
                        self.assertEqual(args[args.index(option) + 1], value)
                    if call["role"] == "luna":
                        self.assertEqual(args[args.index("-m") + 1],
                                         value if overrides == "--luna" else "gpt-6-luna")
                    elif overrides == "--sol":
                        self.assertEqual(args[args.index("-m") + 1], value)
                    else:
                        self.assertEqual(args[args.index("-m") + 1], "gpt-6-sol")
                    if overrides != "--profile":
                        self.assertNotIn("-p", args)
                    if overrides != "--provider":
                        self.assertNotIn("--provider", args)


if __name__ == "__main__":
    unittest.main()
