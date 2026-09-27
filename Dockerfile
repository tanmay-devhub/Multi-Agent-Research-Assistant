# Hardened runtime image for the RAA API (§12).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Non-root user (fixed uid, no shell login needed).
RUN useradd --create-home --uid 10001 appuser

# Install pinned runtime deps first (better layer caching); no dev/test tooling in the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code only. Secrets are NEVER baked in — they are provided via environment at runtime.
COPY app ./app

USER appuser
EXPOSE 8000

# Container binds 0.0.0.0 inside its own namespace; host exposure is controlled by the orchestrator.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
