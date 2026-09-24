# Runtime packaging

Build from this repository, without mounting an agent's host profile:

```sh
docker build -t doer-service:local .
docker run --rm --entrypoint /usr/local/bin/hermes doer-service:local chat --help
docker run --rm --entrypoint /opt/laya-venv/bin/python doer-service:local -c 'from laya import Router; import torch; assert torch.version.cuda is None'
```

The image contains immutable `/app` source, a CPU-only Laya environment at
`/opt/laya-venv`, and Hermes plus its editable source at `/opt/hermes-agent`.
Hermes is pinned to `749220ef0007f8d87bd1531f1c24b0fe93816385` and installed
using its own committed `uv.lock`, separately from this repository's lock.
Python is the 3.12 Debian bookworm image; uv is 0.11.6. Base images and OS
repositories use version tags, not digest pins: this is NOT a bit-reproducible build.
The default runtime user is UID/GID 10001. `/data` holds the SQLite ledger
and job workspaces; `/runtime/hermes` is a dedicated service profile.
The entrypoint only seeds the Doer skill when absent; it does not configure credentials.

## Checkpoint staging (explicit large download)

No checkpoint is downloaded at image build or service startup. Stage the English
checkpoint separately with this repository's locked dependencies installed:

```sh
uv sync --frozen --no-dev
uv run --frozen python scripts/stage_laya.py /absolute/empty/laya-english
```

This uses public `convaiinnovations/laya` revision
`55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`, including weights, encoder,
tokenizer and router configuration. Mount that directory read-only at
`/models/laya` and set `DOER_LAYA_MODEL_DIR=/models/laya`.
The English weights are about 843 MB on disk, with approximately 1.7 GB
FP32 parameter storage before loading peaks, activations and Hermes overhead.
An 8 GiB container budget is a planning allowance, not a measured minimum.
Cynder's 512 MiB class does not fit this local model. Choose an actually
available larger resource class before deployment; no Cynder deployment is implied.
No multilingual/typed checkpoint, remote Jev service or GPU is required.

## Runtime configuration

Supply these non-secret values through the environment:

- `DOER_ORIGIN`: public HTTPS origin, no path
- `DOER_FACILITATOR`: trusted HTTPS x402 v2 facilitator base URL
- `DOER_NETWORK`, `DOER_ASSET`, `DOER_PAY_TO`, `DOER_AMOUNT`: exact EVM chain,
  ERC-20 token address, recipient, and atomic positive price
- `DOER_PAYMENT_EXTRA`: EIP-712 domain JSON (USDC example: `{"name":"USD Coin","version":"2"}`)
- `DOER_PROFILE=default`, `DOER_PROVIDER`, `DOER_MODEL`, `DOER_LUNA`: explicitly
  selected, available runtime identities; choosing a name does not create entitlement
- `DOER_HERMES_HOME=/runtime/hermes`, `DOER_LAYA_MODEL_DIR=/models/laya`
- optional `DOER_JOB_TIMEOUT` (default 1300 seconds), `DOER_CAPACITY` (default 4)
- optional `DOER_DB` and `DOER_WORKSPACES` (defaults `/data/doer.sqlite`, `/data/workspaces`)

Provision only the dedicated service profile using approved credential custody.
Do not mount a fleet agent profile or a wallet key. The worker deliberately strips
ambient environment credentials; provider configuration must be in that dedicated
profile. Settlement credentials are not supported or exposed to model tools.
Use an accessible trusted facilitator with the documented unauthenticated HTTPS
verify/settle interface, or add separately reviewed authenticated transport.

Run with a writable, quota-limited `/data` volume, private `/runtime`, a read-only
model mount, explicit memory/CPU/PID limits and no Docker socket or host mounts.
Expose port 8080 only behind TLS and a trusted-customer ingress policy. The service
is not a sandbox: the worker and controller share a Unix identity and potentially
accessible files. Hostile prompts require a separate isolated worker architecture.
See [service.md](service.md) for payment nonce/signing, job tokens, replay,
settlement uncertainty, limits and result semantics.

## Verification boundaries

`python -m pytest -q -p no:cacheprovider` runs offline controller, synthetic x402/HTTP and
subprocess lifecycle tests. Paid facilitator settlement, provider credentials,
checkpoint inference and a deployed Cynder transaction are separate checks and
are not proven by fake facilitator tests or `/ready`.
