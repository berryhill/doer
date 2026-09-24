# Doer

## Paid HTTP service

The optional [paid service](docs/service.md) exposes Doer through an x402 v2
endpoint with durable jobs, private results, intent-bound signed nonces, bounded
execution and a single worker. See [runtime packaging](docs/runtime.md) for the
pinned Hermes/Laya image and setup. It is a trusted-customer prototype, not a
multi-tenant sandbox. Container/source verification is separate from live model
access, Cynder hosting eligibility and an approved on-chain paid invocation.

Doer runs one task per invocation. It asks a typed decision model whether the requested outcome is concrete and observable, selects an implementation model once, judges the implementation report, and independently checks a user-selected file. On a failed check, Luna diagnoses the failure and the same selected model retries with the original request preserved. It makes at most three implementation attempts; reaching the limit is not success.

## Run one task

Run `./doer` from the repository root. The executable launches `cli.py` with this project's `.venv` Python. The default decision backend, Laya, loads a Router directly in the controller process, reuses it for the decisions in that task, and exits with the task. There is no Laya server, port, or background process to start. Its first live run may download the checkpoint into the Hugging Face cache; it needs no TypeSafe API key. Implementation and Luna assessment run as separate Hermes chat invocations; this is not a gateway bot or `/doer` command. No dedicated Doer profile is required. A live run still needs a working `hermes` CLI and Hermes authentication/model access configured. Both chats inherit the current Hermes profile, credentials, and provider; implementation uses the selected model, while diagnosis and final assessment select `gpt-6-luna` by default.

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

The workspace must exist for a live run. `--execute` invokes models and allows the selected implementer to act; use a new disposable directory for each live task. `--verify-file` is required for live runs and names a relative artifact path inside the workspace. The independent verifier requires a regular file, rejects absolute paths, parent escapes, and symlinks in the path, and optionally compares its UTF-8 contents with `--expect-text` after stripping leading and trailing whitespace. Without `--expect-text`, it checks file presence only. `--help` lists the remaining options.

After a passing gate, the stable default is `gpt-6-sol`. Experimental family routing requires `--routing-policy laya` (or deterministic `--routing-policy rules`) and repeated `--available-model NAME` values confirmed for the selected account/profile. Doer does not discover entitlements itself: these are operator assertions, and the default Sol assumption is explicitly marked unverified in the trace. `--sol MODEL` bypasses the router. Only GPT-6 Astra, Sol, Luna and their Hermes `-900k` aliases are candidates; aliases are local long-context variants stripped before Codex requests, not provider-returned IDs. A provider/profile override does not establish availability.

The trace's `model_selection` evidence records availability, capability constraints, initial implementer-prompt byte count, context-fit uncertainty, family proposal, fallback, and final name; `sol_implementation.model` records the Hermes-reported actual name. Bytes are not tokens and do not include future tool reads or retry feedback. Laya proposes a family, not an alias; its confidence is uncalibrated. With no measured token count, Doer does not select an alias. Optional `--observed-input-tokens N` is an operator-reported measurement (not independently verified); values above the conservative normal allowance require a confirmed `-900k` alias or selection stops safely. A low-confidence/invalid family falls back to confirmed Sol; if Sol is unavailable, it stops rather than guessing. The `--routing-policy laya` mode is opt-in because outcome-grounded evidence has not shown an advantage over stable Sol.

Optional overrides: `--profile NAME` selects a Hermes profile, `--provider NAME` selects a provider for both chats, `--sol MODEL` bypasses routing and uses that implementation model, and `--luna MODEL` replaces the default `gpt-6-luna` diagnosis/final-assessment model. Profile and provider remain ambient unless supplied. Overrides do not configure credentials or guarantee access.

The optional `--backend jev` uses the official TypeSafe HTTPS API instead of local Laya and requires `TYPESAFE_API_KEY` configured securely outside this repository. For example, append `--backend jev` to the live command above once credentials are available. Do not paste credentials into chat or commit them here. No live Jev call is documented as tested.

## Agent Skill

The portable Agent Skill is in [`doer-loop/SKILL.md`](doer-loop/SKILL.md). A clone contains the file but does not automatically install this root-level skill into Hermes. Preview and install it from the public GitHub repository:

```sh
hermes skills inspect berryhill/doer/doer-loop
hermes skills install berryhill/doer/doer-loop
```

Review the security scan before confirming installation. If you already have a user-local `doer-loop` skill, inspect it before replacing anything; a clone alone does not replace your local copy. You can validate the source with `skills-ref validate ./doer-loop`. The skill describes safe invocation and independent verification; it does not install Doer, Laya, or Hermes model access.

## Results and retries

The initial gate checks whether the task and expected result are specified; an unclear contract yields `needs_input` without selection or an implementation attempt. After each implementation attempt, the decision model judges its report for completion, scope drift, fulfillment, working evidence, and reasonable practices; the separate file check must also pass for `verified`. Failures feed Luna's diagnosis into the next attempt with the same model. Exhausting three attempts yields `incomplete`, not success. At the end of the run, Luna assesses completion quality and confidence in one sentence based on the observed verdicts and verification evidence. The JSON `completion` contains that sentence for `verified`, `incomplete`, or `needs_input`; a model/controller exception produces `error` and still attempts the Luna report. If Luna fails, `completion_error` explains why without changing the outcome or exit status. The CLI prints JSON and exits nonzero unless its status is `verified`.

The JSON `trace` lists gate, staged model selection (availability, constraints, context fit, family proposal, selected name and uncertainty), implementation (actual Hermes-reported model), decision judgment, file verification, Luna diagnosis (if needed), and Luna completion steps in execution order. Each step records `kind`, `attempt`, elapsed wall-clock seconds, model, provider, evidence/error, and for Hermes calls a session ID and token counts from stream-json. `usd`, `cost_status` and `cost_source` reflect the primary model usage row for the session in the read-only Hermes profile database (`--profile` when explicitly supplied; otherwise the default profile). Provider is labeled `ambient` if not explicitly selected, rather than guessing. Costs can be actual, estimated, subscription-included, or unknown; unrelated automatic tasks such as title generation are not attributed to an individual step. Local Laya and the file verifier have no billed API charge, but CPU, electricity, downloads, and subscription fees are not priced. `known_cost_usd` is a subtotal of priced model steps; `unknown_cost` is true if a remote step cannot be priced. `total_elapsed_seconds` covers the controller lifecycle. At completion Doer also emits a deterministic human-readable `report` with status, attempts, Luna assessment/error, every step's model/provider, timing, tokens, verdict/evidence/error and cost status, plus totals. It prints the same report to stderr so people see the stats without parsing JSON; stdout remains one valid JSON object for callers. The report repeats trace contents and is not an independent check. Traces and reports can contain task text and artifact contents; handle both streams accordingly.

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

Laya judges Sol's report, not the actual quality, safety, or full behavior of the resulting code.
Routing is not calibrated. `routing_eval.py` provides fifteen diagnostic cases with staged source snapshots, executable checks, and a held-out subset; these are hypotheses, not best-model labels. `eval_model_selection.py --results-dir "$DIR"` reports unmeasured cases honestly. To collect a matched outcome, run one `--run-case ID --policy sol` (then `rules`, then `laya`) with `--results-dir "$DIR" --provider openai-codex` per policy in a tracked process, supplying confirmed `--available-model` values for non-Sol modes. Each run gets a fresh workspace; retained controller JSON, stdout/stderr, exit code, and independent acceptance are aggregated by the summary command. Do not treat a controller file check alone as whole-loop success. In three measured paired cases (two development, one held-out), all policies passed independent checks; Laya selected Sol or fell back to it, while the rules baseline used Luna on the simple text case. No Laya advantage was demonstrated, and the remaining twelve cases were unmeasured. Keep experimental routing opt-in. Its `answer_confidence` is not calibrated for Doer tasks: the installed checkpoint emitted a calibration-temperature warning and missed scope drift in a small probe despite high selected-answer probability (see `RESEARCH.md` and `eval_laya.py`). Jev uses its own `confidence` field; neither score is a guarantee. The independent verifier checks only one chosen file and optional stripped text, not other files, side effects, test-suite results, or whether Sol stayed inside the workspace. An instruction to work inside the workspace is not a security sandbox. Hermes chats can inherit ambient profile configuration, credentials, tools, and permissions; check these before a live run. Doer does not add an automatic yolo flag, but that does not neutralize permissive ambient configuration. Use isolated disposable workspaces and human review; avoid consequential or untrusted tasks without additional controls.

See `RESEARCH.md` for design context and `cli.py`, `doer.py`, and `decisions.py` for current behavior. Laya: https://github.com/NandhaKishorM/laya · Jev: https://docs.typesafe.ai/introduction/quickstart
