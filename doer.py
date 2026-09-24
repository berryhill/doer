"""One task, up to three implementation attempts. The judge never executes actions."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Gate:
    specified: bool
    result_defined: bool


@dataclass(frozen=True)
class Verdict:
    done: bool
    drift: bool
    fulfills: bool
    works: bool
    practices: bool = True

    def failures(self) -> tuple[str, ...]:
        return tuple(name for name, bad in (
            ("done", not self.done), ("drift", self.drift),
            ("fulfills", not self.fulfills), ("works", not self.works),
            ("practices", not self.practices)
        ) if bad)


@dataclass(frozen=True)
class Result:
    status: str
    attempts: int
    message: str
    work: str = ""


def run(task: str, gate: Callable, implement: Callable, judge: Callable,
        diagnose: Callable, max_attempts: int = 3, verify: Callable | None = None) -> Result:
    """Run a fixed contract; always stop on the attempt limit, not necessarily success."""
    if not task.strip():
        return Result("needs_input", 0, "What should I do?")
    if max_attempts < 1 or max_attempts > 3:
        raise ValueError("max_attempts must be between 1 and 3")
    contract = gate(task)
    if not (contract.specified and contract.result_defined):
        missing = tuple(k for k, present in (("specified", contract.specified),
                                              ("result_defined", contract.result_defined)) if not present)
        return Result("needs_input", 0, diagnose(task, "", missing))
    prompt = task
    for attempt in range(1, max_attempts + 1):
        work = implement(prompt)
        verdict = judge(task, work)
        failed = verdict.failures()
        if verify is not None and not verify(task, work):
            failed += ("independent_verification",)
        if not failed:
            if verify is not None:
                return Result("verified", attempt, "Model checks and independent verifier passed.", work)
            return Result("judged_pass", attempt, "Jev passed the reported checks; external result not independently verified.", work)
        if attempt == max_attempts:
            return Result("incomplete", attempt, "Stopped at attempt limit; failed: " + ", ".join(failed), work)
        feedback = diagnose(task, work, failed)
        prompt = (f"Original request (unchanged):\n{task}\n\n"
                  f"Previous attempt:\n{work}\n\n"
                  f"Issues to address (not a new user request):\n{feedback}\n"
                  "Stay within the original request. Recheck the result with real evidence.")
    raise AssertionError("unreachable")
