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
    def test_family_choice_never_includes_context_aliases(self):
        candidates = ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
        class FakeRouter:
            answer = {"choice": "gpt-6-astra", "answer_confidence": 0.92}
            def predict(self, state, questions):
                self.questions = questions
                return {"answers": {"implementation_family": self.answer}}
        router = FakeRouter()
        client = make_client()
        object.__setattr__(client, "_router", router)
        selected = client.choose_family("Build a complex parser", candidates)
        self.assertEqual(selected["choice"], "gpt-6-astra")
        self.assertEqual(set(router.questions["implementation_family"]["criteria"]), set(candidates))
        criteria = router.questions["implementation_family"]["criteria"]
        self.assertIn("typo", criteria["gpt-6-luna"])
        self.assertIn("architecture", criteria["gpt-6-astra"])
        for answer in ({"choice": "gpt-6-astra", "answer_confidence": 0.3},
                       {"choice": "unknown", "answer_confidence": 0.99}, {}):
            router.answer = answer
            with self.subTest(answer=answer):
                result = client.choose_family("task", candidates)
                self.assertEqual(result["choice"], answer.get("choice"))
                self.assertEqual(result["confidence"], answer.get("answer_confidence"))

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

    def test_jev_family_choice_uses_its_own_confidence_and_same_allowed_choices(self):
        client = DecisionClient("jev", "https://api.typesafe.ai/v1/systemone", "secret")
        candidates = ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
        with patch("decisions.urllib.request.urlopen", return_value=Response({"answers": {
            "implementation_family": {"choice": "gpt-6-luna", "confidence": 0.85}}})) as send:
            result = client.choose_family("Fix a typo", candidates)
        self.assertEqual(result["choice"], "gpt-6-luna")
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(set(payload["questions"]["implementation_family"]["criteria"]), set(candidates))

    def test_laya_never_requires_endpoint(self):
        self.assertIsNone(make_client("laya").url)


if __name__ == "__main__": unittest.main()
