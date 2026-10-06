# Small, no compiler, no root.
FROM python:3.11-slim AS base

LABEL org.opencontainers.image.title="Reckon" \
      org.opencontainers.image.description="Matches incoming payments to invoices, and hands a person the cases it is not sure about." \
      org.opencontainers.image.source="https://github.com/MelvTheGoat/Stack"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

# The fitted model ships with the image, so suggestions work with no training
# step on boot. The practice corpus ships too, for RECON_DEMO=1.
COPY fixtures ./fixtures
COPY models ./models

RUN useradd --create-home --uid 10001 recon && chown -R recon /app
USER recon

# The host sets PORT. Default is for running it locally.
#
# No database URL is baked in. Without one the books go in a SQLite file
# inside the container, which the setup page warns is wiped on redeploy; with
# DATABASE_URL (what Railway and similar hosts set when you attach Postgres)
# they go there instead.
ENV PORT=8080

EXPOSE 8080
# --proxy-headers: the host terminates HTTPS in front of us, and the setup
# page has to show Paystack an https:// webhook address, not http://.
CMD exec uvicorn recon.app:app --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --forwarded-allow-ips="*"
