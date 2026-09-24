import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from cli import verify_file


class AdapterTests(unittest.TestCase):
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
            stdout = "READY"
            stderr = ""
        with patch("cli.subprocess.run", return_value=Proc()) as invoke:
            self.assertEqual(hermes("test", None, "/work"), "READY")
        command = invoke.call_args.args[0]
        self.assertEqual(command, ["hermes", "chat", "--query-file", "-", "-Q",
                                   "--source", "tool", "--in", "/work",
                                   "--max-turns", "20", "--run-budget", "300"])
        self.assertEqual(invoke.call_args.kwargs["input"], "test")

    def test_hermes_forwards_only_explicit_overrides(self):
        from cli import hermes
        class Proc:
            returncode = 0
            stdout = "READY"
            stderr = ""
        with patch("cli.subprocess.run", return_value=Proc()) as invoke:
            hermes("test", "custom-model", "/work", profile="custom", provider="custom-provider")
        command = invoke.call_args.args[0]
        self.assertEqual(command, ["hermes", "-p", "custom", "chat", "--query-file", "-", "-Q",
                                   "-m", "custom-model", "--provider", "custom-provider",
                                   "--source", "tool", "--in", "/work",
                                   "--max-turns", "20", "--run-budget", "300"])

    def test_local_repair_probe_forces_exactly_one_failed_verification(self):
        from cli import local_repair_probe
        verify = local_repair_probe(lambda task, work: True)
        self.assertFalse(verify("task", "work"))
        self.assertTrue(verify("task", "work"))


if __name__ == "__main__": unittest.main()
