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


def make_client(backend: str = "laya") -> DecisionClient:
    if backend == "laya":
        return DecisionClient("laya")
    if backend == "jev":
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise ValueError("TYPESAFE_API_KEY is required for Jev; use default Laya otherwise")
        return DecisionClient("jev", JEV_URL, key)
    raise ValueError("backend must be jev or laya")
