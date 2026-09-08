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

API_HOST="$(python - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path
for raw in Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if line.startswith("API_HOST="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"

if [[ -z "$API_HOST" ]]; then
  echo "API_HOST missing after validation" >&2
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

echo "==> verifying dependency health inside the running API container"
compose exec -T api python scripts/probe_dependencies.py

echo "==> verifying public HTTPS health"
curl --fail --silent --show-error \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${API_HOST}/health" >/dev/null

echo "deployment verified: https://${API_HOST}/health"
