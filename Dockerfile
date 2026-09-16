# hpcusage web application. TLS is terminated in front (App Runner / ALB / Caddy);
# the container speaks plain HTTP on :8000.
#
# Layer order is chosen for cheap pushes: system packages and Python dependencies come first
# and only change when pyproject.toml changes; the source code (small, changes often) comes
# last, so a code-only rebuild pushes a few hundred KB to ECR, not the ~100 MB of dependencies.
#
# Stages:
#   plotly   – fetches the vendored plotly.min.js (own stage => stable layer digest, pushed once)
#   base     – runtime deps + source + Makefile/collector/scripts (make targets work inside the container)
#   dev      – base + dev extras (pytest, ruff, responses, ...); used by docker compose locally
#   runtime  – production image (default build target; what `make push` sends to ECR)
FROM curlimages/curl:8.10.1 AS plotly
RUN curl -fsSL https://cdn.plot.ly/plotly-2.35.2.min.js -o /tmp/plotly.min.js

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUFF_CACHE_DIR=/tmp/ruff-cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl make \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 hpcusage

# 1. Dependencies only — cached until pyproject.toml changes.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt

# 2. Source — small layers that change often. Editable install so the compose bind mount + --reload
#    works with the same image; --no-deps because step 1 already installed everything.
COPY README.md Makefile ./
COPY app ./app
COPY collector ./collector
COPY scripts ./scripts
RUN pip install --no-deps -e .
COPY --from=plotly /tmp/plotly.min.js app/hpcusage/static/plotly.min.js

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD curl -fsS http://localhost:8000/healthz || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "hpcusage.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------------------------------------------------------------------------
FROM base AS dev
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']))" \
        > /tmp/requirements-dev.txt \
    && pip install -r /tmp/requirements-dev.txt
USER hpcusage

# ---------------------------------------------------------------------------
FROM base AS runtime
USER hpcusage
