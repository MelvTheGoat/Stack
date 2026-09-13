# Small, no compiler, no root.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The corpus and the fitted model ship with the image: the demo has to work
# with no database and no training step on boot.
COPY fixtures ./fixtures
COPY models ./models

RUN useradd --create-home --uid 10001 recon && chown -R recon /app
USER recon

# Cloud Run sets PORT. Default is for running it locally.
ENV PORT=8080 \
    RECON_DATABASE_URL=sqlite+pysqlite:////tmp/recon.db \
    RECON_OFFLINE=1

EXPOSE 8080
CMD exec uvicorn recon.app:app --host 0.0.0.0 --port ${PORT}
