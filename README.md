# Doer

Doer runs one task per invocation. It asks a typed decision model whether the requested outcome is concrete and observable, sends implementation to Sol, judges Sol's report, and independently checks a user-selected file. On a failed check, Luna diagnoses the failure and Sol retries with the original request preserved. It makes at most three implementation attempts; reaching the limit is not success.

## Run one task

Run `./doer` from the repository root. The executable launches `cli.py` with this project's `.venv` Python. The default decision backend, Laya, loads a Router directly in the controller process, reuses it for the decisions in that task, and exits with the task. There is no Laya server, port, or background process to start. Its first live run may download the checkpoint into the Hugging Face cache; it needs no TypeSafe API key. Sol and Luna run as separate Hermes chat invocations; this is not a gateway bot or `/doer` command. No dedicated Doer profile is required. A live run still needs a working `hermes` CLI and Hermes authentication/model access configured. By default, both chats inherit the current Hermes default profile/config, credentials, model, and provider; they may use the same configured model.

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

Optional overrides: `--profile NAME` selects a Hermes profile, `--provider NAME` selects a provider for both chats, and `--sol MODEL` / `--luna MODEL` select their respective chat models. Only supplied overrides are forwarded; an omitted option continues to use ambient Hermes configuration. These options do not configure credentials or guarantee access to the selected models.

The optional `--backend jev` uses the official TypeSafe HTTPS API instead of local Laya and requires `TYPESAFE_API_KEY` configured securely outside this repository. For example, append `--backend jev` to the live command above once credentials are available. Do not paste credentials into chat or commit them here. No live Jev call is documented as tested.

## Agent Skill

The portable Agent Skill is in [`doer-loop/SKILL.md`](doer-loop/SKILL.md). A clone contains the file but does not automatically install this root-level skill into Hermes. Preview and install it from the public GitHub repository:

```sh
hermes skills inspect berryhill/doer/doer-loop
hermes skills install berryhill/doer/doer-loop
```

Review the security scan before confirming installation. If you already have a user-local `doer-loop` skill, inspect it before replacing anything; a clone alone does not replace your local copy. You can validate the source with `skills-ref validate ./doer-loop`. The skill describes safe invocation and independent verification; it does not install Doer, Laya, or Hermes model access.

## Results and retries

The initial gate checks whether the task and expected result are specified; an unclear contract yields `needs_input` without an implementation attempt. After each Sol attempt, the decision model judges Sol's report for completion, scope drift, fulfillment, working evidence, and reasonable practices; the separate file check must also pass for `verified`. Failures feed Luna's diagnosis into the next Sol attempt. Exhausting three attempts yields `incomplete`, not success. At the end of the run, Luna assesses completion quality and confidence in one sentence based on the observed verdicts and verification evidence. The JSON `completion` contains that sentence for `verified`, `incomplete`, or `needs_input`; a model/controller exception produces `error` and still attempts the Luna report. If Luna fails, `completion_error` explains why without changing the outcome or exit status. The CLI prints JSON and exits nonzero unless its status is `verified`.

The JSON `trace` lists gate, Sol implementation, decision judgment, file verification, Luna diagnosis (if needed), and Luna completion steps in execution order. Each step records `kind`, `attempt`, elapsed wall-clock seconds, model, provider, evidence/error, and for Hermes calls a session ID and token counts from stream-json. `usd`, `cost_status` and `cost_source` reflect the primary model usage row for the session in the read-only Hermes profile database (`--profile` when explicitly supplied; otherwise the default profile). Provider is labeled `ambient` if not explicitly selected, rather than guessing. Costs can be actual, estimated, subscription-included, or unknown; unrelated automatic tasks such as title generation are not attributed to an individual step. Local Laya and the file verifier have no billed API charge, but CPU, electricity, downloads, and subscription fees are not priced. `known_cost_usd` is a subtotal of priced model steps; `unknown_cost` is true if a remote step cannot be priced. `total_elapsed_seconds` covers the controller lifecycle. Traces can contain task text and artifact contents; handle the JSON accordingly.

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

The opt-in test invokes real models and requires Hermes authentication/model access (no special profile); do not run it expecting an offline check.

## Safety and accuracy limits

Laya judges Sol's report, not the actual quality, safety, or full behavior of the resulting code. Its `answer_confidence` is not calibrated for Doer tasks: the installed checkpoint emitted a calibration-temperature warning and missed scope drift in a small probe despite high selected-answer probability (see `RESEARCH.md` and `eval_laya.py`). Jev uses its own `confidence` field; neither score is a guarantee. The independent verifier checks only one chosen file and optional stripped text, not other files, side effects, test-suite results, or whether Sol stayed inside the workspace. An instruction to work inside the workspace is not a security sandbox. Hermes chats can inherit ambient profile configuration, credentials, tools, and permissions; check these before a live run. Doer does not add an automatic yolo flag, but that does not neutralize permissive ambient configuration. Use isolated disposable workspaces and human review; avoid consequential or untrusted tasks without additional controls.

See `RESEARCH.md` for design context and `cli.py`, `doer.py`, and `decisions.py` for current behavior. Laya: https://github.com/NandhaKishorM/laya · Jev: https://docs.typesafe.ai/introduction/quickstart
