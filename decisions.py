"""Typed decisions: Laya directly in the controller, Jev over HTTPS when selected."""
from dataclasses import dataclass, field
import json
import os
import urllib.request

JEV_URL = "https://api.typesafe.ai/v1/systemone"
YES_NO = {"type": "choice", "criteria": {
    "yes": "yes, supported by the supplied state",
    "no": "no or insufficient evidence",
}}


@dataclass(frozen=True)
class DecisionClient:
    backend: str
    url: str | None = None
    api_key: str | None = None
    _router: object | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.backend == "laya":
            if self.url is not None:
                raise ValueError("Laya runs directly; no server URL is accepted")
        elif self.backend == "jev":
            if self.url != JEV_URL:
                raise ValueError("Jev endpoint must be the official HTTPS API")
            if not self.api_key:
                raise ValueError("TYPESAFE_API_KEY is required for Jev")
        else:
            raise ValueError("backend must be jev or laya")

    def ask(self, state: str, questions: dict[str, str]) -> dict[str, bool]:
        schema = {name: {**YES_NO, "instructions": instruction}
                  for name, instruction in questions.items()}
        if self.backend == "laya":
            if self._router is None:
                from laya import Router
                object.__setattr__(self, "_router", Router())
            answers = self._router.predict(state, schema)["answers"]
            score, threshold = "answer_confidence", 0.6
        else:
            body = {"state": state, "model": "jev-latest", "questions": schema}
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + self.api_key},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=90) as response:
                answers = json.load(response)["answers"]
            score, threshold = "confidence", 0.8
        return {name: (answers.get(name, {}).get("choice") == "yes" and
                       answers.get(name, {}).get(score, 0) >= threshold)
                for name in questions}

    def route(self, task: str, candidates: tuple[str, ...]) -> dict:
        """Choose one bounded implementation model; fail closed to Sol on weak output."""
        guidance = {
            "gpt-6-luna": "Simple, localized task such as a typo, small text file or mechanical edit; minimal reasoning.",
            "gpt-6-sol": "Ordinary coding: bug fix, parser, tests, CLI feature or moderate multi-file change.",
            "gpt-6-astra": "Difficult reasoning, security architecture, concurrency design or cross-system tradeoffs.",
        }
        schema = {"implementation_model": {
            "type": "choice",
            "instructions": "Choose the least costly capable model for this task: Luna for simple edits, Sol for ordinary coding, Astra for complex architecture or difficult reasoning; use a 900k alias only if the task needs unusually long context. Pick one exact option; do not invent names.",
            "criteria": {name: (guidance[name.removesuffix("-900k")] +
                                 (" Requires unusually long context (900k)." if name.endswith("-900k") else
                                  " Normal context fits.")) for name in candidates},
        }}
        state = json.dumps({"task": task, "candidates": candidates})
        if self.backend == "laya":
            if self._router is None:
                from laya import Router
                object.__setattr__(self, "_router", Router())
            answer = self._router.predict(state, schema).get("answers", {}).get("implementation_model", {})
            score, threshold = "answer_confidence", 0.6
        else:
            req = urllib.request.Request(self.url, data=json.dumps({
                "state": state, "model": "jev-latest", "questions": schema}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
                method="POST")
            with urllib.request.urlopen(req, timeout=90) as response:
                answer = json.load(response).get("answers", {}).get("implementation_model", {})
            score, threshold = "confidence", 0.8
        if not isinstance(answer, dict):
            answer = {}
        choice, confidence = answer.get("choice"), answer.get(score)
        valid = (isinstance(choice, str) and choice in candidates and
                 isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and
                 0 <= confidence <= 1 and confidence >= threshold)
        return {"model": choice if valid else "gpt-6-sol", "choice": choice,
                "confidence": confidence, "reason": "selected" if valid else "fallback"}


def make_client(backend: str = "laya") -> DecisionClient:
    if backend == "laya":
        return DecisionClient("laya")
    if backend == "jev":
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise ValueError("TYPESAFE_API_KEY is required for Jev; use default Laya otherwise")
        return DecisionClient("jev", JEV_URL, key)
    raise ValueError("backend must be jev or laya")
