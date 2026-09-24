"""One-task Doer runner, with local Laya decisions by default."""
import argparse
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from decisions import make_client
from doer import Gate, Result, Verdict, run


def hermes(prompt: str, model: str | None, workspace: str,
           profile: str | None = None, provider: str | None = None) -> str:
    # stdin avoids shell interpolation of arbitrary user text.
    command = ["hermes"]
    if profile is not None:
        command.extend(["-p", profile])
    command.extend(["chat", "--query-file", "-", "-Q"])
    if model is not None:
        command.extend(["-m", model])
    if provider is not None:
        command.extend(["--provider", provider])
    command.extend(["--source", "tool", "--in", workspace,
                    "--max-turns", "20", "--run-budget", "300"])
    proc = subprocess.run(command, input=prompt,
                          text=True, capture_output=True, timeout=360)
    if proc.returncode:
        raise RuntimeError(f"Hermes ({model}) failed: {proc.stderr[-1200:]}")
    return proc.stdout.strip()


def verify_file(workspace: str, relative: str, expected: str | None) -> bool:
    """Check a user-chosen artifact without following links or escaping workspace."""
    root = Path(workspace).resolve()
    candidate = root / relative
    if Path(relative).is_absolute():
        return False
    try:
        parts = candidate.relative_to(root).parts
        if ".." in parts or not parts:
            return False
        for parent in (root.joinpath(*parts[:n]) for n in range(1, len(parts) + 1)):
            if parent.is_symlink():
                return False
        if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
            return False
        return expected is None or candidate.read_text(encoding="utf-8").strip() == expected.strip()
    except (OSError, UnicodeError, ValueError):
        return False


def local_repair_probe(verifier):
    """Force one failed check for an opt-in local retry smoke, then use real checks."""
    first = True

    def check(task, work):
        nonlocal first
        if first:
            first = False
            return False
        return verifier(task, work)

    return check


def main() -> int:
    parser = argparse.ArgumentParser(description="One task, at most three Sol attempts")
    parser.add_argument("task", help="The single task prompt")
    parser.add_argument("--workspace", required=True, help="Directory where Sol may work")
    parser.add_argument("--execute", action="store_true", help="Actually invoke models and allow Sol to act")
    parser.add_argument("--verify-file", help="Required relative artifact path for live run")
    parser.add_argument("--expect-text", help="Optional exact expected text after stripping outer whitespace")
    parser.add_argument("--backend", choices=("laya", "jev"), default="laya",
                        help="Typed decision backend (default: in-process Laya)")
    parser.add_argument("--test-retry-once", action="store_true",
                        help="Local Laya smoke only: inject one failed verification to exercise Luna repair")
    parser.add_argument("--profile", help="Optional Hermes profile override")
    parser.add_argument("--provider", help="Optional Hermes provider override")
    parser.add_argument("--sol", help="Optional Sol model override")
    parser.add_argument("--luna", help="Optional Luna model override")
    args = parser.parse_args()
    if not args.execute:
        print(f"Dry run ({args.backend}): pass --execute to send this task to the decision model and Hermes. No changes made.")
        return 0
    if not args.verify_file:
        parser.error("--execute requires --verify-file for an independent completion check")
    if args.test_retry_once and args.backend != "laya":
        parser.error("--test-retry-once is only available with local Laya")
    try:
        decisions = make_client(args.backend)
    except ValueError as exc:
        parser.error(str(exc))
    workspace = os.path.realpath(args.workspace)
    if not os.path.isdir(workspace):
        parser.error("--workspace must name an existing directory")

    def gate(task):
        state = ("Task: " + task + "\nExpected result: " + args.verify_file +
                 " exists in the workspace" +
                 (" and contains exactly " + repr(args.expect_text)
                  if args.expect_text is not None else "") + ".")
        answers = decisions.ask(state, {
            "specified": "Does the user request one concrete actionable outcome, rather than only discuss an idea?",
            "result_defined": "Does the request identify an observable result or artifact that could count as success?"
        })
        return Gate(**answers)

    def implement(prompt):
        return hermes("One task in workspace " + workspace + ". Required artifact: " +
                      args.verify_file + ". Expected exact text (if set): " + repr(args.expect_text) +
                      ". Original scope and subsequent diagnosis are below. "
                      "Work only inside the workspace. Do not commit, "
                      "push, deploy or write to external systems. Execute relevant checks, "
                      "then report the actual artifact paths and observed test output. "
                      "If blocked, report the blocker; never invent success.\n\n" + prompt,
                      args.sol, workspace, profile=args.profile, provider=args.provider)

    def judge(task, work):
        answers = decisions.ask(json.dumps({"request": task, "implementer_report": work}), {
            "done": "Does the report describe a completed result rather than merely an intention or partial work?",
            "stays_in_scope": "Does the reported work stay within the original request, with no unrelated changes?",
            "fulfills": "Does the reported result address the requested outcome?",
            "works": "Does the report include specific observed checks or evidence that the result works?",
            "practices": "Does the report show reasonable task-appropriate practices (such as relevant testing and no obvious unsafe shortcut), without demanding perfection?"
        })
        return Verdict(answers["done"], not answers["stays_in_scope"],
                       answers["fulfills"], answers["works"], answers["practices"])

    def diagnose(task, work, failed):
        return hermes("Original task:\n" + task + "\n\nRequired artifact: " + args.verify_file +
                      "\nExpected exact text (if set): " + repr(args.expect_text) +
                      "\n\nPrevious report:\n" + work +
                      "\n\nFailed checks: " + ", ".join(failed) +
                      "\nIdentify the missing input if this is a contract failure, or the "
                      "smallest concrete repair for the next implementation attempt. "
                      "Do not perform the task. Do not invent evidence.", args.luna, workspace,
                      profile=args.profile, provider=args.provider)

    verifier = lambda task, work: verify_file(workspace, args.verify_file, args.expect_text)
    if args.test_retry_once:
        verifier = local_repair_probe(verifier)
    result = run(args.task, gate, implement, judge, diagnose, verify=verifier)
    print(json.dumps(result.__dict__, indent=2))
    return 0 if result.status == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
