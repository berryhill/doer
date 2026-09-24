# Paid Doer service

This repository adds a single-replica, single-worker HTTP service around the real Doer CLI. It is a trusted-customer prototype, NOT a sandbox for anonymous prompts. See [runtime packaging](runtime.md) for installation and container setup.

## Contract

- `POST /doer`: JSON `{"task":"Create a short document","verify_file":"result.md"}`; optional `expect_text` performs the CLI's stripped exact-text check. No client-provided command, environment, profile, provider or model.
- `GET /jobs/{id}`: persisted status and safe payment receipt.
- `GET /jobs/{id}/result`: UTF-8 text artifact only after verified completion.
- `GET /health`: process liveness.
- `GET /ready`: database and worker readiness, explicitly NOT proof of model access, checkpoint availability or settlement connectivity.

Before the initial POST, the caller generates and privately retains a random 32-byte URL-safe `X-Job-Token` (43 or more characters) and a fresh `Idempotency-Key` (16–128 alphanumeric/underscore/hyphen characters). Send the same token, key and body on every retry, including the unpaid request. Only the token hash is stored. Possession of an idempotency key or payment header alone cannot retrieve results. Losing a paid HTTP response does NOT lose the capability: repeat the original request to retrieve the same job and stored receipt. Losing the caller's token does lose access; do not make another payment to recover it.

Example unpaid local request (synthetic task; the endpoint must already be configured):

```sh
JOB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
JOB_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
curl -i http://127.0.0.1:8080/doer \
  -H 'Content-Type: application/json' \
  -H "X-Job-Token: $JOB_TOKEN" -H "Idempotency-Key: $JOB_KEY" \
  --data '{"task":"Create result.txt containing hello","verify_file":"result.txt","expect_text":"hello"}'
```

Keep tokens out of shell logs/process inspection in shared environments; use a protected client in real operation. Never send payment headers to plaintext public HTTP.

## x402 v2 and signed intent binding

An unpaid request returns HTTP 402 with base64 JSON `PAYMENT-REQUIRED`. Requirements use `exact`, configured `eip155:<chain>`, EIP-3009 asset, atomic `amount`, `payTo`, and token-domain `extra.name/version`. The service supports EIP-3009 authorization payloads, not Permit2 or Solana. The trusted facilitator implements standard `POST /verify` and `/settle`. HTTPS is mandatory; redirects are rejected. There is no production mock or bypass mode.

The `doer-intent` extension provides `info.nonce`, `bodySha256`, `tokenSha256`, method/path and idempotency-key hash. The client MUST use this exact 32-byte nonce in the EIP-3009 authorization before signing. This is an application-specific nonce convention: generic x402 clients which always choose an unrelated random nonce need an adapter. Resource/extension echoes alone are NOT signatures. The nonce is signed by EIP-3009 and commits to the immutable job, body, recipient/amount/network/asset requirement, origin, key and result-token hash. The server rejects a signature/nonce copied to another intent before calling the facilitator. The caller must inspect the entire challenge and trust its TLS origin before signing.

Submit the same body/key/token with base64 `PAYMENT-SIGNATURE`, matching `accepted`, `resource`, and `payload.authorization`. Authorization contains from/to/value/nonce/validAfter/validBefore; validity is bounded to 300 seconds. A 65-byte EVM signature is required. The facilitator remains responsible for cryptographic signature recovery, funds/time checks and chain settlement. The server cross-checks payer, requirement and settlement network/amount/transaction shape. It does not independently reimplement blockchain consensus. Payment identity is unique by network/token/payer/nonce (NOT by task, so a second task cannot defeat nonce deduplication).

After confirmed settlement, HTTP 202 returns the job and base64 `PAYMENT-RESPONSE`; authenticated replay returns the durable receipt without another settlement. Results are private via `X-Job-Token`. No raw payment signature is persisted or passed to Hermes.

## Durable lifecycle and bounded execution

SQLite FULL-synchronous WAL stores intents, token hashes, payment identity hashes, receipts and result text. A filesystem lock enforces one controller per database before restart recovery. Capacity is checked and reserved atomically immediately before payment verification; an old 402 challenge does not reserve a paid slot. Challenges may still be issued while workers are busy. Full execution capacity returns 503 BEFORE settlement. Distinct bodies with an existing key return 409.

A failed verification is terminal `payment_failed`; the payment is not settled. A settlement timeout, malformed receipt, false success, empty transaction or contradictory payer/network/amount is conservatively `settlement_unknown`, never an invitation to pay again. No automated financial retry or fabricated refund. Preserve the same job and reconcile facilitator/chain evidence out of band. Running/verifying jobs become `interrupted` after restart; settling becomes `settlement_unknown`; settled queued jobs may resume. A crash after settlement but before its durable receipt may remain unknown. Do not resubmit under a fresh key to escape it.

Payment buys one bounded Doer execution, not a guaranteed successful artifact. `needs_input`, `incomplete`, `error`, `failed`, `deadline` and cancellation are not success. Even `verified` means the existing Doer judgment plus a bounded selected-file check—not general code quality or absence of side effects. No auto-refund is implemented.

Defaults: 8192-byte request; 4000-character task; safe 160-character ASCII relative artifact path; 32768-byte text artifact; four admitted jobs, one running worker; 64 unpaid intents; 10000 total durable records. Expiration is a state transition, never hard deletion. Lifetime record/storage limits intentionally fail closed; operator retention/backup is needed for extended use. History/workspaces are retained, not automatically erased.

Worker controls include whole-job timeout, bounded captured output, cancellation and process-group cleanup. Workspace monitoring is a bounded best-effort guard, NOT a hard filesystem quota or security sandbox. Use a quota-limited volume and container CPU/memory/PID limits. Separate model runtime home from the controller/payment database; do not mount fleet profiles, host home, Docker socket or unrelated secrets. The same-UID trusted prototype can still access its container filesystem; untrusted customers require a separately isolated executor and credential broker before exposure.

## Configuration

Required: `DOER_ORIGIN` (HTTPS origin), `DOER_FACILITATOR` (HTTPS base URL, optional path prefix), `DOER_NETWORK`, `DOER_ASSET`, `DOER_PAY_TO`, `DOER_AMOUNT` (positive atomic token units), `DOER_PROFILE=default`, `DOER_PROVIDER`, `DOER_MODEL`, `DOER_LUNA`. The default identity is the dedicated service home, never the host default profile.

Optional: `DOER_PAYMENT_EXTRA` JSON EIP-712 `name/version`, `DOER_DB=/data/doer.sqlite`, `DOER_WORKSPACES=/data/workspaces`, `DOER_HERMES_HOME=/runtime/hermes`, `DOER_JOB_TIMEOUT=1300` (1–3600 seconds), `DOER_CAPACITY=4` (1–32), `PORT=8080`. `DOER_LAYA_MODEL_DIR` selects a staged pinned English checkpoint with CPU-only single-checkpoint routing. Without it, the ordinary CLI retains upstream Router download behavior; use the pinned path for deployment.

Model authentication is provisioned into the dedicated runtime home, outside the image and repository. No wallet private key is needed by this resource server. Payment recipient/network/price are non-secret operator configuration, not examples to invent. Use a trusted facilitator compatible with the deployment; this adapter currently supports unauthenticated HTTPS facilitator endpoints (no CDP JWT generation).

## Cynder boundary

Cynder's paid control-plane DEPLOY/INVOKE is separate from public workload HTTP. Exposing this service on public HTTP does NOT cause Cynder to apply per-request x402; this service performs that check. No deployment or spend is authorized by building it.

The packaged Cynder snapshot advertises a small policy with 512Mi memory, 1Gi ephemeral storage and 300-second workload timeout. Default local Laya alone exceeds that memory/storage budget; this service also needs durable SQLite state and a worker that stays alive between HTTP requests. Do NOT deploy this as a scale-to-zero, request-only container with ephemeral database storage. A suitable persistent, always-on resource class/lease, secret materialization, outbound model/facilitator access and sufficient memory must be verified against the live Cynder API before quoting/deploying. This repository does not change Cynder policy or claim those prerequisites exist.

## Verification

```sh
mkdir -p .scratch/temp
uv sync --frozen
TMPDIR="$PWD/.scratch/temp" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Offline tests use the actual loopback HTTP handler, synthetic facilitator responses, and synthetic subprocess controllers. They do not prove blockchain settlement or real Hermes inference. The existing opt-in model smoke remains separate. A paid acceptance test requires exact quote approval, a configured live facilitator/recipient, actual settlement receipt, actual Doer artifact, and restart/replay evidence. Do not equate an HTTP 200 readiness probe, fake payment fixture, or image build with that live proof.
