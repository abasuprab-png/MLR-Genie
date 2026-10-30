# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY evals ./evals
# The optional CA is supplied by managed build environments only. Pip keeps TLS
# verification enabled; this session-specific trust bundle never enters a layer.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MLR_GENIE_ENV=hosted \
    PORT=8080 \
    DATABASE_URL=sqlite:////data/mlr_genie_authoritative.db
WORKDIR /app
COPY --from=build /wheels /wheels
# Install the wheel's runtime packages beside the writable artifact directory.
# Application code remains root-owned; only data paths belong to the runtime UID.
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        --target=/app mlr-genie==3.4.0 \
    && rm -rf /wheels \
    && groupadd --gid 10001 mlrgenie \
    && useradd --uid 10001 --gid mlrgenie --no-create-home --home-dir /app mlrgenie \
    && mkdir -p /app/artifacts/file_roundtrip /data \
    && chown -R mlrgenie:mlrgenie /app/artifacts /data
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "backend.server", "--host", "0.0.0.0"]
