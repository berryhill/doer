"""Offline packaging and staged-checkpoint contracts, not live inference."""
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import types
import unittest
from unittest.mock import patch, Mock

import decisions

ROOT = Path(__file__).resolve().parent


class PackagingTests(unittest.TestCase):
    def test_cpu_lock_matches_manifest(self):
        manifest = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        lock = tomllib.loads((ROOT / 'uv.lock').read_text())
        self.assertFalse(manifest['tool']['uv']['package'])
        packages = {p['name']: p for p in lock['package']}
        self.assertEqual(packages['laya']['version'], '0.3.20')
        self.assertEqual(packages['torch']['version'], '2.4.1+cpu')
        self.assertEqual(packages['torch']['source']['registry'], 'https://download.pytorch.org/whl/cpu')
        self.assertFalse(any(n.startswith('nvidia-') for n in packages))

    def test_docker_input_closure(self):
        text = (ROOT / 'Dockerfile').read_text()
        for line in text.splitlines():
            if line.startswith('COPY ') and '--from=' not in line:
                for source in line.split()[1:-1]:
                    self.assertTrue((ROOT / source).is_file(), source)
        self.assertIn('USER 10001:10001', text)
        self.assertIn('uv sync --frozen --no-dev', text)
        self.assertIn('749220ef0007f8d87bd1531f1c24b0fe93816385', text)
        self.assertIn('!worker.py', (ROOT / '.dockerignore').read_text())
        self.assertNotIn('service.py.snapshot', text)

    def test_staged_router_uses_only_local_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'model.safetensors').touch()
            constructor = Mock(return_value=Mock())
            with patch.dict(sys.modules, {'laya': types.SimpleNamespace(Router=constructor)}), \
                    patch.dict(os.environ, {'DOER_LAYA_MODEL_DIR': tmp}):
                router = decisions.make_router()
                decisions.router_predict(router, 'task', {'gate': {}})
            constructor.assert_called_once_with(models={'english': tmp}, device='cpu', max_loaded=1, preload=False)
            router.predict.assert_called_once_with('task', {'gate': {}}, model='english')

    def test_missing_checkpoint_fails_closed(self):
        constructor = Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(sys.modules, {'laya': types.SimpleNamespace(Router=constructor)}), \
                patch.dict(os.environ, {'DOER_LAYA_MODEL_DIR': tmp}):
            with self.assertRaises(ValueError):
                decisions.make_router()
        constructor.assert_not_called()

    def test_ordinary_cli_keeps_default_router(self):
        constructor = Mock()
        with patch.dict(sys.modules, {'laya': types.SimpleNamespace(Router=constructor)}), \
                patch.dict(os.environ, {}, clear=True):
            decisions.make_router()
        constructor.assert_called_once_with()
