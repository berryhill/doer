# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.6 AS uv
FROM python:3.12-slim-bookworm AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy TMPDIR=/build-tmp
RUN mkdir -p /build-tmp && apt-get update && apt-get install -y --no-install-recommends ca-certificates git libgomp1 tini

FROM base AS laya
WORKDIR /opt/laya
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/laya-venv uv sync --frozen --no-dev --no-progress
RUN /opt/laya-venv/bin/python -c "from laya import Router; import torch; assert torch.version.cuda is None"

FROM base AS hermes
ARG HERMES_PIN=749220ef0007f8d87bd1531f1c24b0fe93816385
WORKDIR /opt/hermes-agent
RUN git init -q && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth=1 origin "${HERMES_PIN}" && git checkout -q FETCH_HEAD
RUN uv sync --frozen --no-dev --no-progress --python /usr/local/bin/python3.12

FROM base AS runtime
COPY --from=laya /opt/laya-venv /opt/laya-venv
# Hermes uses an editable installation; retain its source at the same path.
COPY --from=hermes /opt/hermes-agent /opt/hermes-agent
WORKDIR /app
COPY cli.py doer.py decisions.py routing.py service.py worker.py ./
COPY doer-loop/SKILL.md /opt/doer-skills/doer-loop/SKILL.md
COPY scripts/bootstrap-runtime.sh /usr/local/bin/doer-bootstrap
RUN chmod 755 /usr/local/bin/doer-bootstrap && ln -s /opt/hermes-agent/.venv/bin/hermes /usr/local/bin/hermes && useradd --uid 10001 --create-home --home-dir /runtime doer && mkdir -p /data /runtime/tmp /runtime/hermes/skills && cp -r /opt/doer-skills/doer-loop /runtime/hermes/skills/ && chown -R doer:doer /runtime /data && chmod -R a-w /app
ENV PATH=/opt/laya-venv/bin:/usr/local/bin:/usr/bin:/bin HOME=/runtime HERMES_HOME=/runtime/hermes TMPDIR=/runtime/tmp HF_HOME=/runtime/hf-cache PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "doer-bootstrap"]
CMD ["/opt/laya-venv/bin/python", "/app/service.py", "--host", "0.0.0.0", "--port", "8080"]
