import json
import sys
import types
import unittest
from unittest.mock import patch
from decisions import DecisionClient, make_client


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.body


class DecisionsTests(unittest.TestCase):
    def test_default_is_in_process_laya_without_typesafe_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            client = make_client()
        self.assertEqual(client.backend, "laya")
        self.assertIsNone(client.url)
        self.assertIsNone(client.api_key)

    def test_laya_router_is_loaded_once_and_reused_without_http(self):
        instances = []
        class FakeRouter:
            def __init__(self): instances.append(self)
            def predict(self, state, questions):
                self.last = (state, questions)
                return {"answers": {"ready": {
                    "choice": "yes", "confidence": 0.2, "answer_confidence": 0.76}}}
        client = make_client()
        with patch.dict(sys.modules, {"laya": types.SimpleNamespace(Router=FakeRouter)}), \
             patch("decisions.urllib.request.urlopen", side_effect=AssertionError("HTTP used")):
            self.assertEqual(client.ask("task", {"ready": "Is it ready?"}), {"ready": True})
            self.assertEqual(client.ask("task2", {"ready": "Is it ready?"}), {"ready": True})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].last[0], "task2")
        self.assertEqual(instances[0].last[1]["ready"]["type"], "choice")

    def test_jev_requires_credential_and_uses_jev_confidence(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
                make_client("jev")
        client = DecisionClient("jev", "https://api.typesafe.ai/v1/systemone", "secret")
        with patch("decisions.urllib.request.urlopen", return_value=Response({"answers": {
            "ready": {"choice": "yes", "confidence": 0.9}}})) as send:
            self.assertEqual(client.ask("task", {"ready": "Is it ready?"}), {"ready": True})
        req = send.call_args.args[0]
        self.assertEqual(json.loads(req.data)["model"], "jev-latest")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret")

    def test_laya_never_requires_endpoint(self):
        self.assertIsNone(make_client("laya").url)


if __name__ == "__main__": unittest.main()
