"""Outcome-grounded Doer comparison; no unmeasured case is a best-model label.

Run each policy on a fresh staged snapshot, then summarize recorded controller
JSON and independent acceptance. Live runs use models and may take minutes.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .routing_eval import CASES

POLICIES = ("sol", "rules", "laya")


def stage(case, work):
    for name, content in case["files"].items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if (work / case["verify_file"]).exists():
        raise ValueError("verification target must be absent before running")


def accept(case, work):
    if case["acceptance"] == "exact":
        path = work / case["verify_file"]
        passed = path.is_file() and not path.is_symlink() and path.read_text().strip() == case["expect_text"]
        return {"passed": passed, "check": "independent_exact_text"}
    if case["acceptance"] == "unittest":
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-q"],
                              cwd=work, text=True, capture_output=True, timeout=30,
                              env={**os.environ, "TZ": "America/Los_Angeles"})
        target = work / case["verify_file"]
        return {"passed": proc.returncode == 0 and target.is_file() and not target.is_symlink(),
                "check": "unittest_discover", "exit_code": proc.returncode,
                "output": (proc.stdout + proc.stderr)[-1200:]}
    raise ValueError("unknown acceptance check")


def run_case(case, policy, results, available, provider=None):
    if policy not in POLICIES:
        raise ValueError("unknown policy")
    if not case["run_live"]:
        raise ValueError("diagnostic-only case; run only after explicit independent review")
    record = results / (case["id"] + "__" + policy + ".json")
    if record.exists():
        raise FileExistsError("record exists; never reuse a prior run")
    work = Path(tempfile.mkdtemp(prefix=case["id"] + "-" + policy + "-", dir=results))
    stage(case, work)
    command = [sys.executable, "-m", "doer_loop.cli", case["task"],
               "--workspace", str(work), "--verify-file", case["verify_file"],
               "--execute", "--routing-policy", policy]
    if case["expect_text"] is not None:
        command.extend(["--expect-text", case["expect_text"]])
    if provider:
        command.extend(["--provider", provider])
    if policy != "sol":
        for model in available:
            command.extend(["--available-model", model])
    proc = subprocess.run(command, text=True, capture_output=True)
    (results / (case["id"] + "__" + policy + ".stdout")).write_text(proc.stdout)
    (results / (case["id"] + "__" + policy + ".stderr")).write_text(proc.stderr)
    (results / (case["id"] + "__" + policy + ".exit-code")).write_text(str(proc.returncode))
    try:
        controller = json.loads(proc.stdout)
    except json.JSONDecodeError:
        controller = None
    result = {"case": case["id"], "policy": policy, "workspace": str(work),
              "exit_code": proc.returncode, "controller": controller,
              "acceptance": accept(case, work)}
    record.write_text(json.dumps(result, indent=2))
    return result


def summarize(results):
    rows = {policy: {"observed": 0, "acceptance_passed": 0, "controller_verified": 0,
                     "attempts": 0, "elapsed_seconds": 0, "tokens": {"input": 0, "output": 0},
                     "known_cost_usd": 0, "unknown_cost": False} for policy in POLICIES}
    unmeasured = []
    paired = []
    for case in CASES:
        outcomes = {}
        for policy in POLICIES:
            path = results / (case["id"] + "__" + policy + ".json")
            if not path.is_file():
                unmeasured.append({"case": case["id"], "policy": policy, "split": case["split"],
                                   "category": case["category"], "label_kind": case["label_kind"]})
                continue
            record = json.loads(path.read_text())
            controller = record.get("controller")
            row = rows[policy]
            row["observed"] += 1
            passed = (record.get("exit_code") == 0 and isinstance(controller, dict) and
                      controller.get("status") == "verified" and record["acceptance"]["passed"])
            outcomes[policy] = passed
            row["acceptance_passed"] += int(passed)
            if isinstance(controller, dict):
                row["controller_verified"] += int(controller.get("status") == "verified")
                row["attempts"] += controller.get("attempts", 0)
                row["elapsed_seconds"] += controller.get("total_elapsed_seconds", 0)
                row["known_cost_usd"] += controller.get("known_cost_usd", 0)
                row["unknown_cost"] |= controller.get("unknown_cost", True)
                for step in controller.get("trace", []):
                    for key in row["tokens"]:
                        row["tokens"][key] += (step.get("tokens") or {}).get(key, 0)
            else:
                row["unknown_cost"] = True
        if len(outcomes) == len(POLICIES):
            paired.append({"case": case["id"], "split": case["split"], "success": outcomes,
                           "tie": len(set(outcomes.values())) == 1})
    return {**rows, "paired": paired, "unmeasured": unmeasured,
            "note": "Only controller exit plus independent acceptance counts; no best-model labels or winner inferred."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--run-case", choices=[c["id"] for c in CASES])
    parser.add_argument("--policy", choices=POLICIES)
    parser.add_argument("--available-model", action="append", default=[])
    parser.add_argument("--provider")
    args = parser.parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    if args.run_case:
        if not args.policy:
            parser.error("--run-case requires --policy")
        if args.policy != "sol" and not args.available_model:
            parser.error("non-Sol policy requires confirmed --available-model values")
        case = next(c for c in CASES if c["id"] == args.run_case)
        result = run_case(case, args.policy, args.results_dir, args.available_model, args.provider)
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(summarize(args.results_dir), indent=2))


if __name__ == "__main__":
    main()
