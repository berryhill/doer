# Doer

Doer runs one task per invocation. It asks a typed decision model whether the requested outcome is concrete and observable, sends implementation to Sol, judges Sol's report, and independently checks a user-selected file. On a failed check, Luna diagnoses the failure and Sol retries with the original request preserved. It makes at most three implementation attempts; reaching the limit is not success.

## Run one task

Run `./doer` from the repository root. The executable launches `cli.py` with this project's `.venv` Python. The default decision backend, Laya, loads a Router directly in the controller process, reuses it for the decisions in that task, and exits with the task. There is no Laya server, port, or background process to start. Its first live run may download the checkpoint into the Hugging Face cache; it needs no TypeSafe API key. Sol and Luna run as separate Hermes conversations through the `doer` Hermes profile; this is not a gateway bot or `/doer` command. A live run needs a working `hermes` CLI and that profile configured with model access.

Use a fresh disposable workspace, not the repository root. The parent directory in this example must exist; it uses the configured `TMPDIR`, or `~/.hermes/cache/scratch` if unset:

```sh
WORKDIR="$(mktemp -d "${TMPDIR:-$HOME/.hermes/cache/scratch}/doer-demo.XXXXXX")"
./doer 'Create result.txt containing exactly hello' --workspace "$WORKDIR"
```

Without `--execute`, Doer prints a dry-run message; it does not call models or modify the workspace. To run the task for real:

```sh
./doer 'Create result.txt containing exactly hello in the workspace. No other files.' \
  --workspace "$WORKDIR" --verify-file result.txt --expect-text hello --execute
```

The workspace must exist for a live run. `--execute` invokes models and allows Sol to act; use a new disposable directory for each live task. `--verify-file` is required for live runs and names a relative artifact path inside the workspace. The independent verifier requires a regular file, rejects absolute paths, parent escapes, and symlinks in the path, and optionally compares its UTF-8 contents with `--expect-text` after stripping leading and trailing whitespace. Without `--expect-text`, it checks file presence only. `--help` lists the remaining options.

The optional `--backend jev` uses the official TypeSafe HTTPS API instead of local Laya and requires `TYPESAFE_API_KEY` configured securely outside this repository. For example, append `--backend jev` to the live command above once credentials are available. Do not paste credentials into chat or commit them here. No live Jev call is documented as tested.

## Results and retries

The initial gate checks whether the task and expected result are specified; an unclear contract yields `needs_input` without an implementation attempt. After each Sol attempt, the decision model judges Sol's report for completion, scope drift, fulfillment, working evidence, and reasonable practices; the separate file check must also pass for `verified`. Failures feed Luna's diagnosis into the next Sol attempt. Exhausting three attempts yields `incomplete`, not success. The CLI prints a JSON result and exits nonzero unless its status is `verified`.

For a retry-path smoke test only, `--test-retry-once` with local Laya forces the first independent check to fail, then uses the real verifier. This deliberately exercises Luna and a second attempt; the injected failure is not evidence of a defect. Do not use it for real tasks.

## Tests

Offline unit and fake-backend integration tests (the real-model smoke is skipped by default):

```sh
.venv/bin/python -m unittest discover -v
```

Opt-in real Laya/Sol smoke, which creates its own disposable temporary workspace using the environment's temporary-directory setting:

```sh
DOER_LIVE_SMOKE=1 .venv/bin/python -m unittest -v test_integration.IntegrationTests.test_real_laya_and_sol_create_verified_artifact
```

The opt-in test invokes real models and requires Hermes profile/model access; do not run it expecting an offline check.

## Safety and accuracy limits

Laya judges Sol's report, not the actual quality, safety, or full behavior of the resulting code. Its `answer_confidence` is not calibrated for Doer tasks: the installed checkpoint emitted a calibration-temperature warning and missed scope drift in a small probe despite high selected-answer probability (see `RESEARCH.md` and `eval_laya.py`). Jev uses its own `confidence` field; neither score is a guarantee. The independent verifier checks only one chosen file and optional stripped text, not other files, side effects, test-suite results, or whether Sol stayed inside the workspace. An instruction to work inside the workspace is not a security sandbox. Use isolated disposable workspaces and human review; avoid consequential or untrusted tasks without additional controls.

See `RESEARCH.md` for design context and `cli.py`, `doer.py`, and `decisions.py` for current behavior. Laya: https://github.com/NandhaKishorM/laya · Jev: https://docs.typesafe.ai/introduction/quickstart
