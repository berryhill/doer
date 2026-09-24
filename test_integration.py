"""Exercise the controller with direct Laya and subprocess Hermes (no server)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent

FAKE_LAYA = '''import os
from pathlib import Path
class Router:
    def __init__(self):
        with Path(os.environ["FAKE_LAYA_LOG"]).open("a") as f: f.write("load\\n")
    def predict(self, state, questions):
        with Path(os.environ["FAKE_LAYA_LOG"]).open("a") as f: f.write("predict " + ",".join(questions) + "\\n")
        return {"answers": {key: {"choice": "yes", "answer_confidence": 0.99}
                            for key in questions}}
'''

FAKE_HERMES = '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv
assert args[1:4] == ["-p", "doer", "chat"] and "--query-file" in args
prompt = sys.stdin.read()
model = args[args.index("-m") + 1]
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as f: f.write(model + " | " + prompt.replace("\\n", " ") + "\\n")
if model == "gpt-6-luna":
    print("Artifact missing; create result.txt with the expected content.")
else:
    count = sum(line.startswith("gpt-6-sol") for line in log.read_text().splitlines())
    if count == 2:
        (Path(args[args.index("--in") + 1]) / "result.txt").write_text("hello\\n")
    print("Implemented; test reports success.")
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

    def test_reported_success_does_not_pass_until_actual_artifact_exists(self):
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
                   "PYTHONPATH": str(bin_dir), "FAKE_LOG": str(log), "FAKE_LAYA_LOG": str(laya_log)}
            completed = subprocess.run([sys.executable, str(ROOT / "cli.py"),
                                        "Create result.txt containing hello", "--workspace", str(work),
                                        "--verify-file", "result.txt", "--expect-text", "hello", "--execute"],
                                       capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertEqual((result["status"], result["attempts"]), ("verified", 2))
            self.assertEqual((work / "result.txt").read_text(), "hello\n")
            calls = log.read_text().splitlines()
            self.assertEqual(sum(c.startswith("gpt-6-sol") for c in calls), 2)
            self.assertEqual(sum(c.startswith("gpt-6-luna") for c in calls), 1)
            self.assertIn("Artifact missing", calls[-1])
            self.assertEqual(laya_log.read_text().splitlines()[0], "load")
            self.assertEqual(len(laya_log.read_text().splitlines()), 4)  # one load, three predictions


if __name__ == "__main__":
    unittest.main()
