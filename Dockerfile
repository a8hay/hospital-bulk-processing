FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

RUN useradd --system --no-create-home appuser
USER appuser

ENV PATH="/srv/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
# One worker: runner tasks, the semaphore and the sweeper live in-process (decision C5).
# Gunicorn supervises it and restarts it if it dies; graceful-timeout lets runners mark batches interrupted.
CMD gunicorn "app.main:create_app()" --worker-class uvicorn.workers.UvicornWorker --workers 1 \
    --bind 0.0.0.0:${PORT} --graceful-timeout 20 --access-logfile -
