# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock .python-version ./
COPY packages/quantlab/pyproject.toml packages/quantlab/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
RUN mkdir -p packages/quantlab/src/quantlab services/api/src/quantlab_api \
 && touch packages/quantlab/src/quantlab/__init__.py services/api/src/quantlab_api/__init__.py \
 && uv sync --frozen --no-dev --no-install-workspace

FROM deps AS runtime
COPY packages packages
COPY services services
RUN uv sync --frozen --no-dev \
 && useradd --create-home --uid 10001 quantlab && mkdir -p /data && chown quantlab /data
# AWS RDS CA bundle, so QUANTLAB_DATABASE_URL can use sslmode=verify-full with
# sslrootcert=/etc/ssl/certs/rds-global-bundle.pem (#19, infra/terraform/README.md).
# Pinned: a mismatch fails the build ("digest mismatch"), and a new AWS bundle is a
# deliberate update of this checksum. 0644 so uid 10001 can read it.
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b --chmod=0644 \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
ENV PATH="/app/.venv/bin:$PATH" QUANTLAB_DATA_DIR=/data
USER quantlab
EXPOSE 8000
CMD ["uvicorn", "quantlab_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
