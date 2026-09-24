"""One-task Doer runner, with local Laya decisions by default."""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from decisions import make_client
from doer import Gate, Result, Verdict, run
from routing import CANDIDATES as MODEL_CANDIDATES, RoutingError, choose_model


@dataclass(frozen=True)
class HermesResponse:
    text: str
    session_id: str
    tokens: dict
    model: str | None = None


def hermes(prompt: str, model: str | None, workspace: str,
           profile: str | None = None, provider: str | None = None) -> HermesResponse:
    # stdin avoids shell interpolation of arbitrary user text.
    command = ["hermes"]
    if profile is not None:
        command.extend(["-p", profile])
    command.extend(["chat", "--query-file", "-", "--format", "stream-json"])
    if model is not None:
        command.extend(["-m", model])
    if provider is not None:
        command.extend(["--provider", provider])
    command.extend(["--source", "tool", "--in", workspace,
                    "--max-turns", "20", "--run-budget", "300"])
    proc = subprocess.run(command, input=prompt,
                          text=True, capture_output=True, timeout=360)
    try:
        events = [json.loads(line) for line in proc.stdout.splitlines()]
        results = [event for event in events if event.get("type") == "result"]
        if len(results) != 1:
            raise ValueError("expected one terminal result event")
        result = results[0]
        if proc.returncode or result.get("exit_code") != 0:
            raise RuntimeError(f"Hermes ({model}) failed: {result.get('error') or proc.stderr[-1200:]}")
        if not isinstance(result.get("session_id"), str) or not isinstance(result.get("tokens"), dict):
            raise ValueError("result missing session_id or tokens")
        system = next((event for event in events if event.get("type") == "system"), {})
        return HermesResponse(result["text"], result["session_id"], result["tokens"],
                              system.get("model") or model)
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Hermes ({model}) invalid stream result: {exc}; {proc.stderr[-500:]}") from exc


def session_cost(session_id: str, db: Path | None = None, profile: str | None = None) -> dict:
    """Read one Hermes session's primary model usage without guessing missing prices."""
    unknown = {"usd": None, "cost_status": "unknown", "cost_source": None}
    if not session_id:
        return unknown
    if db is None:
        if profile and profile != "default":
            if not all(c.isalnum() or c in "_-" for c in profile):
                return unknown
            db = Path.home() / ".hermes/profiles" / profile / "state.db"
        else:
            db = Path.home() / ".hermes/state.db"
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute("SELECT estimated_cost_usd, actual_cost_usd, cost_status, cost_source "
                                "FROM session_model_usage WHERE session_id = ? AND task = ''",
                                (session_id,)).fetchall()
    except (sqlite3.Error, OSError):
        return unknown
    if not rows or any(row[2] not in ("actual", "estimated", "included") or
                       (row[2] == "actual" and row[1] is None) or
                       (row[2] == "estimated" and row[0] is None) for row in rows):
        return unknown
    return {"usd": sum(row[1] if row[2] == "actual" else row[0] if row[2] == "estimated" else 0
                       for row in rows),
            "cost_status": rows[0][2] if len({row[2] for row in rows}) == 1 else "mixed",
            "cost_source": rows[0][3] if len({row[3] for row in rows}) == 1 else "mixed"}


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


def format_report(payload: dict, exit_code: int) -> str:
    """Render the final recorded outcome without estimating missing usage or prices."""
    def text(value):
        return "unavailable" if value is None else str(value)

    def cost(step):
        status = step["cost_status"]
        if status == "local_no_billed_api":
            return "local unpriced compute (no billed API charge)"
        if status == "unknown" or step["usd"] is None:
            return "unknown"
        label = {"included": "subscription-included API charge",
                 "actual": "actual cost", "estimated": "estimated cost",
                 "mixed": "mixed recorded cost"}.get(status, text(status))
        source = step.get("cost_source")
        return f"{label} (${step['usd']} recorded" + (f"; source: {source})" if source else ")")

    completion = ("Luna completion: " + payload["completion"] if payload["completion"] is not None
                  else "Luna completion error: " + text(payload["completion_error"]))
    lines = [f"Outcome: {payload['status']}; exit code: {exit_code}; attempts: {payload['attempts']}",
             completion, "Message: " + text(payload["message"]), "Trace (execution order):"]
    for index, step in enumerate(payload["trace"], 1):
        tokens = (json.dumps(step["tokens"], sort_keys=True) if step["tokens"] is not None
                  else "unavailable")
        line = (f"{index}. {step['kind']}; attempt: {text(step['attempt']) if step['attempt'] is not None else 'none'}"
                f"; model: {text(step['model'])}; provider: {text(step['provider'])}"
                f"; elapsed: {text(step['elapsed_seconds'])}s; tokens: {tokens}; cost: {cost(step)}")
        if step["evidence"] is not None:
            line += "; evidence: " + json.dumps(step["evidence"], sort_keys=True)
        if step["error"] is not None:
            line += "; error: " + step["error"]
        lines.append(line)
    lines.append(f"Total elapsed: {payload['total_elapsed_seconds']}s; "
                 f"known cost: ${payload['known_cost_usd']}; unknown cost: "
                 + ("yes (total cost unknown)" if payload["unknown_cost"] else "no"))
    return "\n".join(lines)


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
    parser.add_argument("--sol", help="Implementation model override (bypasses automatic routing)")
    parser.add_argument("--routing-policy", choices=("sol", "rules", "laya"), default="sol",
                        help="Implementation routing (default: stable always-Sol; Laya is experimental)")
    parser.add_argument("--available-model", action="append", choices=MODEL_CANDIDATES,
                        help="Repeat for each candidate confirmed on the active provider/profile")
    parser.add_argument("--observed-input-tokens", type=int,
                        help="Operator-reported measured input tokens (not verified by Doer)")
    parser.add_argument("--luna", default="gpt-6-luna",
                        help="Diagnosis and final-assessment model (default: gpt-6-luna)")
    args = parser.parse_args()
    if not args.execute:
        print(f"Dry run ({args.backend}): pass --execute to send this task to the decision model and Hermes. No changes made.")
        return 0
    if not args.verify_file:
        parser.error("--execute requires --verify-file for an independent completion check")
    if args.test_retry_once and args.backend != "laya":
        parser.error("--test-retry-once is only available with local Laya")
    if args.sol is None and args.routing_policy != "sol" and not args.available_model:
        parser.error("experimental routing requires --available-model confirmed on this account")
    try:
        decisions = make_client(args.backend)
    except ValueError as exc:
        parser.error(str(exc))
    workspace = os.path.realpath(args.workspace)
    if not os.path.isdir(workspace):
        parser.error("--workspace must name an existing directory")

    trace = []
    started = time.monotonic()
    decision_model = "laya" if args.backend == "laya" else "jev-latest"
    decision_provider = "local" if args.backend == "laya" else "jev"

    def record(kind, model, provider, action, attempt=None):
        tick = time.monotonic()
        step = {"kind": kind, "attempt": attempt, "elapsed_seconds": None,
                "model": model, "provider": provider, "session_id": None,
                "tokens": None, "usd": None, "cost_status": "unknown", "cost_source": None,
                "evidence": None, "error": None}
        try:
            value = action()
            if isinstance(value, HermesResponse):
                step.update(model=value.model or model, session_id=value.session_id,
                            tokens=value.tokens, **session_cost(value.session_id, profile=args.profile))
                step["evidence"] = value.text
            else:
                step["evidence"] = value
                if provider == "local":
                    step.update(cost_status="local_no_billed_api", cost_source="local")
            return value
        except Exception as exc:
            step["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, RoutingError):
                step["evidence"] = {"stages": exc.stages, "reason": str(exc)}
            if provider == "local":
                step.update(cost_status="local_no_billed_api", cost_source="local")
            raise
        finally:
            step["elapsed_seconds"] = time.monotonic() - tick
            trace.append(step)

    def attempt_number():
        return sum(s["kind"] == "sol_implementation" for s in trace)

    def gate(task):
        state = ("Task: " + task + "\nExpected result: " + args.verify_file +
                 " exists in the workspace" +
                 (" and contains exactly " + repr(args.expect_text)
                  if args.expect_text is not None else "") + ".")
        questions = {
            "specified": "Does the user request one concrete actionable outcome, rather than only discuss an idea?",
            "result_defined": "Does the request identify an observable result or artifact that could count as success?"
        }
        answers = record("gate", decision_model, decision_provider,
                         lambda: decisions.ask(state, questions))
        return Gate(**answers)

    def implementation_text(prompt):
        return ("One task in workspace " + workspace + ". Required artifact: " +
                args.verify_file + ". Expected exact text (if set): " + repr(args.expect_text) +
                ". Original scope and subsequent diagnosis are below. "
                "Work only inside the workspace. Do not commit, "
                "push, deploy or write to external systems. Execute relevant checks, "
                "then report the actual artifact paths and observed test output. "
                "If blocked, report the blocker; never invent success.\n\n" + prompt)

    def select(task):
        def choose():
            return choose_model(task, args.available_model or ("gpt-6-sol",),
                                len(implementation_text(task).encode("utf-8")),
                                decisions.choose_family if args.routing_policy == "laya" and args.sol is None
                                else lambda *_: None,
                                override=args.sol, policy=args.routing_policy,
                                observed_input_tokens=args.observed_input_tokens,
                                availability_source=("operator_confirmed_not_rechecked" if args.available_model
                                                     else "default_unverified"),
                                min_confidence=0.8 if args.backend == "jev" else 0.6)
        step = record("model_selection", decision_model if args.routing_policy == "laya" and args.sol is None
                      else "deterministic", decision_provider if args.routing_policy == "laya" and args.sol is None
                      else "local", choose)
        return step["model"]

    def implement(prompt, model):
        text = implementation_text(prompt)
        return record("sol_implementation", model, args.provider or "ambient",
                      lambda: hermes(text, model, workspace, profile=args.profile,
                                     provider=args.provider), attempt_number() + 1).text

    def judge(task, work):
        questions = {
            "done": "Does the report describe a completed result rather than merely an intention or partial work?",
            "stays_in_scope": "Does the reported work stay within the original request, with no unrelated changes?",
            "fulfills": "Does the reported result address the requested outcome?",
            "works": "Does the report include specific observed checks or evidence that the result works?",
            "practices": "Does the report show reasonable task-appropriate practices (such as relevant testing and no obvious unsafe shortcut), without demanding perfection?"
        }
        answers = record("laya_judgment" if args.backend == "laya" else "jev_judgment",
                         decision_model, decision_provider,
                         lambda: decisions.ask(json.dumps({"request": task, "implementer_report": work}),
                                               questions), attempt_number())
        return Verdict(answers["done"], not answers["stays_in_scope"],
                       answers["fulfills"], answers["works"], answers["practices"])

    def diagnose(task, work, failed):
        prompt = ("Original task:\n" + task + "\n\nRequired artifact: " + args.verify_file +
                  "\nExpected exact text (if set): " + repr(args.expect_text) +
                  "\n\nPrevious report:\n" + work +
                  "\n\nFailed checks: " + ", ".join(failed) +
                  "\nIdentify the missing input if this is a contract failure, or the "
                  "smallest concrete repair for the next implementation attempt. "
                  "Do not perform the task. Do not invent evidence.")
        return record("luna_diagnosis", args.luna, args.provider or "ambient",
                      lambda: hermes(prompt, args.luna, workspace, profile=args.profile,
                                     provider=args.provider), attempt_number()).text

    verifier = lambda task, work: verify_file(workspace, args.verify_file, args.expect_text)
    if args.test_retry_once:
        verifier = local_repair_probe(verifier)

    def check(task, work):
        return record("independent_verifier", "file_check", "local",
                      lambda: verifier(task, work), attempt_number())

    def complete(outcome):
        evidence = {"status": outcome.status, "attempts": outcome.attempts,
                    "message": outcome.message, "implementer_report": outcome.work,
                    "observed_control_trace": trace}
        prompt = ("Give exactly ONE sentence assessing completion quality and confidence based only "
                  "on this actual outcome, Laya/Jev verdicts, independent verification and observed "
                  "evidence; Laya confidence is uncalibrated and a passing single-file check is "
                  "not proof of full quality, so do not claim high confidence solely from those; "
                  "acknowledge limitations and failures without inventing certainty. "
                  "Do not perform any task or modify files.\n" + json.dumps(evidence))
        response = record("luna_completion", args.luna, args.provider or "ambient",
                          lambda: hermes(prompt, args.luna, workspace, profile=args.profile,
                                         provider=args.provider))
        text = response.text.strip()
        if not text or text[-1] not in ".!?" or len(re.split(r"(?<=[.!?])\s+(?=\S)", text)) != 1:
            raise ValueError("Luna completion must be one sentence")
        return text

    try:
        result = run(args.task, gate, implement, judge, diagnose, verify=check,
                     complete=complete, select=select)
    except Exception as exc:
        result = Result("error", attempt_number(), f"{type(exc).__name__}: {exc}")
        try:
            result = replace(result, completion=complete(result))
        except Exception as report_exc:
            result = replace(result, completion_error=f"{type(report_exc).__name__}: {report_exc}")
    exit_code = 0 if result.status == "verified" else 1
    payload = {**result.__dict__, "trace": trace,
               "total_elapsed_seconds": time.monotonic() - started,
               "known_cost_usd": sum(step["usd"] for step in trace if step["usd"] is not None),
               "unknown_cost": any(step["cost_status"] == "unknown" for step in trace)}
    payload["report"] = format_report(payload, exit_code)
    print(json.dumps(payload, indent=2))
    sys.stderr.write(payload["report"] + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
