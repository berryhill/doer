"""Small labelled routing probe; diagnostic, not a calibrated benchmark.

Run with the project venv; compares Laya with a deterministic always-Sol baseline.
"""
import json
from cli import MODEL_CANDIDATES
from decisions import make_client

CASES = (
    ("write a one-line text file", "gpt-6-luna", "Create note.txt containing exactly hello."),
    ("fix a typo", "gpt-6-luna", "Correct the spelling of recieve in README.md."),
    ("rename a variable", "gpt-6-luna", "Rename a local variable in a short function and run its unit test."),
    ("unit-tested parser", "gpt-6-sol", "Implement a CSV parser with quoted fields and unit tests."),
    ("debug a failing test", "gpt-6-sol", "Find and fix the cause of a failing Python unit test and verify it."),
    ("add a CLI option", "gpt-6-sol", "Add a CLI flag with validation, tests and documentation."),
    ("redesign concurrent scheduler", "gpt-6-astra", "Redesign a distributed scheduler's retry protocol to avoid duplicate execution during partitions; prove safety and test failure modes."),
    ("security architecture", "gpt-6-astra", "Design and implement a multi-tenant authorization boundary with threat model and migration strategy."),
)


def family(model):
    return model.removesuffix("-900k")


def main():
    client = make_client()
    rows = []
    for label, expected, task in CASES:
        decision = client.route(task, MODEL_CANDIDATES)
        actual = family(decision["model"])
        rows.append({"label": label, "expected": expected, "baseline": "gpt-6-sol",
                     "laya": actual, "raw_choice": decision["choice"],
                     "confidence": decision["confidence"], "reason": decision["reason"],
                     "baseline_misroute": expected != "gpt-6-sol", "laya_misroute": actual != expected})
    print(json.dumps({"cases": rows, "baseline_misroutes": sum(r["baseline_misroute"] for r in rows),
                      "laya_misroutes": sum(r["laya_misroute"] for r in rows)}, indent=2))


if __name__ == "__main__":
    main()
