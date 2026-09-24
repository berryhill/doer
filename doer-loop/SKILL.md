---
name: doer-loop
description: Run or test the Doer one-task agent loop in a repository checkout, including its Laya decisions, Sol retries, Luna report, trace, and independent artifact verification.
---

# Doer Loop

## When to use

Use for a request to run or test this repository's `./doer` executable. Work from the repository root, and read `README.md` and the relevant controller code for the current behavior. This skill describes the checkout's executable; it does not require an installed skill, a gateway command, or a dedicated Doer profile.

## Run one task

1. Choose an existing, fresh, disposable workspace separate from the repository root. State one concrete task and a relative artifact path to check; if exact text is required, specify it too. Do not use an existing artifact as evidence of a new result.
2. From the repository root, invoke `./doer 'Create result.txt containing exactly hello in the workspace. No other files.' --workspace "$WORKSPACE" --verify-file result.txt --expect-text hello --execute`, with `WORKSPACE` set to that existing disposable directory. The executable uses this checkout's `.venv/bin/python`; use it only when the project environment and Hermes CLI/model access are already available. Without `--execute`, the command is only a dry run and makes no task changes. A live run requires `--verify-file` and an existing workspace.
3. Laya is the default typed decision backend, loaded in-process for the task; there is no Laya server to start. Hermes runs separate implementation, diagnosis, and final-assessment chats, using the ambient model unless explicitly overridden. At most three implementation attempts occur. A failed gate yields `needs_input` without an attempt, and reaching the attempt limit yields `incomplete`, not success.
4. By default Sol and Luna inherit the ambient Hermes profile, provider, credentials, and model configuration; they can share the same configured model. Supply only intended overrides: `--profile NAME`, `--provider NAME`, `--sol MODEL`, and `--luna MODEL`. Overrides do not create model access or credentials. `--backend jev` is optional only when its API credentials are already securely configured outside the repository; do not put credentials in prompts or files.

## Interpret and verify

- Read the CLI JSON `status`, `attempts`, `completion`, and `trace`, and check the exit status: only `verified` exits successfully. `completion` is Luna's one-sentence assessment when available; `completion_error` records a failed completion report without changing the outcome. `error` indicates a model/controller failure. The ordered per-step trace includes gate, Sol implementation, Laya/Jev judgment, independent verifier, Luna diagnosis when needed, and Luna completion, with timing, evidence/errors, model/provider, and available Hermes session, token, and cost data. Do not infer unreported costs or treat model confidence as proof.
- Doer's independent verifier checks only one user-selected regular file inside the workspace. It rejects absolute paths, parent escapes, and symlink paths. Without `--expect-text` it checks presence; with it, it compares UTF-8 text after stripping outer whitespace. It does not validate other artifacts, behavior, test results, side effects, or code quality. Laya judges Sol's report, not the actual implementation. No prompt or verdict is a sandbox: Hermes may inherit ambient tools and permissions. Review the workspace and run task-specific checks independently before reporting success.
- For an artifact, independently inspect the chosen file in the disposable workspace and execute relevant project tests or behavior checks. For this checkout's offline suite, run `.venv/bin/python -m unittest discover -v` from the repository root if that interpreter is already available. The optional real-model smoke is `DOER_LIVE_SMOKE=1 .venv/bin/python -m unittest -v test_integration.IntegrationTests.test_real_laya_and_sol_create_verified_artifact`; it invokes live models and needs working Hermes access. `--test-retry-once` intentionally forces the first check to fail for a local Laya retry smoke; never use it for a real task.
- Compare the source checkout's `git status --short` before and after a live run and inspect the workspace for unrelated changes. In the final response show the actual content of a small, safe text artifact (not only its path), plus status, attempts, and observed checks; use a safe excerpt or path for sensitive or large artifacts. Report failures and blockers plainly. Do not ask Doer to install dependencies, commit, push, deploy, or write to external systems; perform any separately authorized publication yourself after independent review. A workspace instruction is not security isolation.
