"""Small hand-labelled Doer probe against in-process Laya.

This is a diagnostic sample, not a calibration set or benchmark.
"""
import json
from laya import Router
from decisions import YES_NO


def classify_laya(answer):
    return answer.get("choice") == "yes" and answer.get("answer_confidence", 0) >= 0.6

GATE = {
    "specified": "Does the user request one concrete actionable outcome, rather than only discuss an idea?",
    "result_defined": "Does the request identify an observable result or artifact that could count as success?",
}
JUDGE = {
    "done": "Does the report describe a completed result rather than merely an intention or partial work?",
    "stays_in_scope": "Does the reported work stay within the original request, with no unrelated changes?",
    "fulfills": "Does the reported result address the requested outcome?",
    "works": "Does the report include specific observed checks or evidence that the result works?",
    "practices": "Does the report show reasonable task-appropriate practices (such as relevant testing and no obvious unsafe shortcut), without demanding perfection?",
}


def request(router, state, questions):
    schema = {name: {**YES_NO, "instructions": instruction} for name, instruction in questions.items()}
    raw = router.predict(state, schema)["answers"]
    return {name: {"choice": x.get("choice"),
                   "answer_confidence": x.get("answer_confidence"),
                   "pass": classify_laya(x)} for name, x in raw.items()}


def main():
    router = Router()
    cases = [
        ("create a specified file", True, "Create result.txt containing exactly hello in the workspace.", "result.txt", "hello"),
        ("write a report", True, "Write report.txt summarizing the README in this workspace.", "report.txt", None),
        ("fix an exact failure", True, "Fix the failing unit test test_math.py::test_add without deleting the test.", "test_math.py", None),
        ("discussion only", False, "Let's discuss whether this architecture is good.", "report.txt", None),
        ("vague improvement", False, "Make it better.", "result.txt", None),
        ("unspecified result", False, "Do something useful.", "result.txt", None),
        ("ambiguous object", False, "Fix that problem.", "result.txt", None),
        ("question only", False, "What does this repository do?", "answer.txt", None),
    ]
    rows = []
    for label, expected, task, artifact, text in cases:
        state = ("Task: " + task + "\nExpected result: " + artifact + " exists in the workspace" +
                 (" and contains exactly " + repr(text) if text is not None else "") + ".")
        got = request(router, state, GATE)
        passed = all(x["pass"] for x in got.values())
        rows.append({"label": label, "expected": expected, "passed": passed, "answers": got})
        print(label, "expected", expected, "actual", passed, {k: (v["choice"], v["answer_confidence"]) for k, v in got.items()})
    reports = [
        ("claimed complete", "Created result.txt containing hello and read back exact text hello."),
        ("plan only", "I will create result.txt and test it soon."),
        ("no file", "I could not create result.txt because the workspace is read-only."),
        ("scope drift", "Created result.txt containing hello, and deleted unrelated files for cleanup."),
    ]
    for label, report in reports:
        got = request(router, json.dumps({"request": "Create result.txt containing exactly hello.",
                                          "implementer_report": report}), JUDGE)
        print(label, {k: (v["choice"], v["answer_confidence"]) for k, v in got.items()})
        rows.append({"label": label, "answers": got})
    print("JSON=" + json.dumps(rows))


if __name__ == "__main__":
    main()
