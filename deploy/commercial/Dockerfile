# syntax=docker/dockerfile:1
# Pin both arguments to verified image digests in the release record before production.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RELAY_ENV=production RELAY_RUNTIME_DIR=/var/lib/relay
RUN groupadd --gid 10001 relay && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/relay relay \
    && mkdir -p /var/lib/relay && chown 10001:10001 /var/lib/relay
COPY --from=build /opt/venv /opt/venv
WORKDIR /app/backend
# An allowlist keeps .runtime, .env, SQLite and private keys out of image layers.
COPY backend/manage.py ./
COPY backend/config ./config
COPY backend/commerce ./commerce
COPY backend/templates ./templates
COPY backend/static ./static
COPY deploy/commercial/entrypoint.sh /usr/local/bin/relay-entrypoint
RUN chmod 0555 /usr/local/bin/relay-entrypoint
RUN find /app/backend -type d -exec chmod 0555 {} + \
    && find /app/backend -type f -exec chmod 0444 {} +
USER 10001:10001
EXPOSE 8016
ENTRYPOINT ["/usr/local/bin/relay-entrypoint"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "127.0.0.1:8016", "--workers", "2", "--threads", "2", "--timeout", "45", "--access-logfile", "-", "--access-logformat", "%(h)s %(m)s %(U)s %(s)s %(b)s", "--error-logfile", "-", "--forwarded-allow-ips", "127.0.0.1"]
