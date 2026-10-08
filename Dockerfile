# Container image for the ELENTA chat service (REQ-001, REQ-011, REQ-066; ADR-012).
#
# Two stages: the builder resolves dependencies from uv.lock (the only stage that
# needs network), the runtime stage holds just Python, the virtualenv and app code.
# After `docker compose build`, the image runs with no internet access (REQ-012).

# Pinned by digest so every build starts from the exact same base (Python 3.12.15, amd64
# at time of pinning). Update deliberately: pull, re-test, change the digest here.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

# --- builder -------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# Same uv version as local development (ADR-014), installed from PyPI rather than
# pulling a second image.
RUN pip install --no-cache-dir uv==0.11.24

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# --frozen: fail if uv.lock is out of date instead of silently re-resolving.
# --no-dev: test tools (pytest, httpx2) stay out of the runtime image.
# --no-install-project: the app is copied as source, not installed as a package.
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime -------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Image hardening from the container scan (TS-009, REQ-122):
# - upgrade only the OS packages that have a published security fix (Trivy: liblzma5,
#   DSA-6549-1); targeted rather than `apt-get upgrade` so builds stay predictable;
# - remove pip: the runtime never installs packages (dependencies come from the builder
#   stage's virtualenv), and the base image's pip carried 6 known CVEs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade liblzma5 \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip

# Fixed non-root UID/GID so file ownership and `id -u` checks are predictable (REQ-066).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app

# PYTHONDONTWRITEBYTECODE: the root filesystem is read-only at runtime (compose.yaml).
# PYTHONUNBUFFERED: logs reach `docker compose logs` immediately.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8000

CMD ["python", "-m", "app"]
