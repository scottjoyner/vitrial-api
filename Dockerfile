FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EVIDENCE_ROOT=/home/vitrial/evidence

RUN groupadd --system --gid 10001 vitrial \
    && useradd --system --uid 10001 --gid vitrial --create-home --home-dir /home/vitrial vitrial \
    && mkdir -p /home/vitrial/evidence

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && chown -R vitrial:vitrial /app /home/vitrial
COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "scripts/probe_dependencies.py", "--quiet"]

# NOTE on --forwarded-allow-ips: in production this API container publishes NO host port;
# its only TCP peer is the Caddy ingress on the internal network, so `*` is reachable only
# from Caddy. The deployment trust boundary is Caddyfile: it now overwrites
# X-Forwarded-For with {remote_host} rather than appending to a client-supplied value.
#
# NOTE on single-process: deliberately NO --workers and NO --limit-max-requests.
# --workers multiplies the database connection budget: settings.py sizes the pool at
# pool_size 10 + max_overflow 5 per process against a server max_connections that
# app/settings.py explicitly says must be "divided, not multiplied". Raising the
# worker count is therefore a change to the database's connection ceiling, not a
# tuning knob, and is out of scope here.
#
# --limit-max-requests is the more tempting of the two and still wrong at one replica:
# uvicorn recycles the only worker, Caddy takes ~45s to notice via health_uri
# (3 consecutive failures at health_interval 15s), and every request in that window is
# a 502. Memory growth is instead bounded by the container mem_limit in
# deploy/compose.production.yml, which kills the container on genuine runaway rather
# than on a schedule. Add worker recycling only together with a second api replica,
# so the drain is invisible.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
