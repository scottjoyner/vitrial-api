#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-}"
if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  echo "usage: $0 /absolute/path/to/vitrial.env" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/deploy/compose.production.yml"

python "$ROOT/scripts/validate_deployment.py" --env-file "$ENV_FILE" --mode production

mapfile -t DEPLOYMENT_IDENTITY < <(python - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

wanted = ("API_HOST", "API_IMAGE", "SERVICE_VERSION")
values = {}
for raw in Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() in wanted:
        values[key.strip()] = value.strip().strip('"').strip("'")
for key in wanted:
    print(values.get(key, ""))
PY
)
API_HOST="${DEPLOYMENT_IDENTITY[0]:-}"
API_IMAGE="${DEPLOYMENT_IDENTITY[1]:-}"
SERVICE_VERSION="${DEPLOYMENT_IDENTITY[2]:-}"

if [[ -z "$API_HOST" || -z "$API_IMAGE" || -z "$SERVICE_VERSION" ]]; then
  echo "deployment identity missing after validation" >&2
  exit 2
fi

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"
}

echo "==> pulling immutable deployment images"
compose pull

echo "==> applying database migrations before application replacement"
compose run --rm migrate

echo "==> starting API and TLS edge"
compose up -d --no-deps api
compose up -d caddy

echo "==> verifying running API container image identity"
API_CONTAINER_ID="$(compose ps -q api)"
if [[ -z "$API_CONTAINER_ID" ]]; then
  echo "API container is not running" >&2
  exit 1
fi
RUNNING_API_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$API_CONTAINER_ID")"
if [[ "$RUNNING_API_IMAGE" != "$API_IMAGE" ]]; then
  echo "running API image does not match requested immutable API_IMAGE" >&2
  exit 1
fi

echo "==> verifying dependency health inside the running API container"
compose exec -T api python scripts/probe_dependencies.py

echo "==> verifying public HTTPS liveness, readiness, and version"
curl --fail --silent --show-error \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${API_HOST}/health" >/dev/null

READY_JSON="$(curl --fail --silent --show-error \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${API_HOST}/ready")"
python - "$SERVICE_VERSION" "$READY_JSON" <<'PY'
import json
import sys

expected = sys.argv[1]
payload = json.loads(sys.argv[2])
if payload.get("status") != "ready":
    raise SystemExit("public readiness endpoint did not report ready")
if payload.get("serviceVersion") != expected:
    raise SystemExit("public readiness endpoint does not match requested SERVICE_VERSION")
if payload.get("database") != "ok":
    raise SystemExit("public readiness endpoint reports PostgreSQL unavailable")
if payload.get("objectStorage") != "ok":
    raise SystemExit("public readiness endpoint reports evidence storage unavailable")
PY

VERSION_JSON="$(curl --fail --silent --show-error \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${API_HOST}/api/v1/version")"
python - "$SERVICE_VERSION" "$VERSION_JSON" <<'PY'
import json
import sys

expected = sys.argv[1]
payload = json.loads(sys.argv[2])
if payload.get("apiVersion") != "v1":
    raise SystemExit("public version endpoint returned unexpected apiVersion")
if payload.get("serviceVersion") != expected:
    raise SystemExit("public version endpoint does not match requested SERVICE_VERSION")
PY

echo "deployment verified: https://${API_HOST} is live + ready (${SERVICE_VERSION}, ${API_IMAGE})"
