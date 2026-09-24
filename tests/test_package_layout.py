import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent


class PackageLayoutTests(unittest.TestCase):
    def test_source_package_cli_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            result = subprocess.run([sys.executable, "-m", "doer_loop.cli", "--help"],
                                    cwd=elsewhere, env=env, capture_output=True,
                                    text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--verify-file", result.stdout)

    def test_project_declares_installed_console_script(self):
        import tomllib
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["scripts"]["doer"], "doer_loop.cli:main")
        self.assertEqual(project["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])

    def test_tests_and_evaluation_live_outside_importable_package(self):
        self.assertTrue((ROOT / "tests" / "test_integration.py").is_file())
        self.assertTrue((ROOT / "tools" / "eval_model_selection.py").is_file())
        self.assertFalse((ROOT / "test_integration.py").exists())
