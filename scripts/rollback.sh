#!/usr/bin/env bash
set -euo pipefail

PREVIOUS_ENV="${1:-}"
if [[ -z "$PREVIOUS_ENV" || ! -f "$PREVIOUS_ENV" ]]; then
  echo "usage: $0 /absolute/path/to/previous-vitrial.env" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/deploy/compose.production.yml"

python "$ROOT/scripts/validate_deployment.py" --env-file "$PREVIOUS_ENV" --mode production

API_HOST="$(python - "$PREVIOUS_ENV" <<'PY'
import sys
from pathlib import Path
for raw in Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if line.startswith("API_HOST="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"

compose() {
  docker compose --env-file "$PREVIOUS_ENV" -f "$COMPOSE" "$@"
}

echo "==> rolling application image back without downgrading the database"
compose pull api
compose up -d --no-deps api
compose up -d caddy
compose exec -T api python scripts/probe_dependencies.py
curl --fail --silent --show-error \
  --retry 12 --retry-delay 5 --retry-all-errors \
  "https://${API_HOST}/health" >/dev/null

echo "application rollback verified; database schema was intentionally not downgraded"
