# Two stages, one image. The Vue build is copied into the Python image and
# served by FastAPI, so production has a single deploy target and a single
# origin: no CORS configuration, no second certificate, no third-party host.

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes to ../backend/static; keep that path valid inside the stage.
RUN mkdir -p /backend/static && sed -i "s#'../backend/static'#'/backend/static'#" vite.config.ts \
    && npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/pyproject.toml ./
RUN pip install --no-cache-dir . && pip uninstall -y pip setuptools || true

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/scripts ./scripts
COPY --from=frontend /backend/static ./static

# Run as a non-root user; Cloud Run does not require it but defence in depth is free.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
# Cloud Run injects $PORT. Single worker: the workload is I/O bound on external
# APIs, and concurrency is handled by the async event loop, not by processes.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
