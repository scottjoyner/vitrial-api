#!/usr/bin/env bash
# SCRUM-39/51/64 — isolated synthetic PostgreSQL acceptance.
set -euo pipefail
if env | grep -Eq '^(DATABASE_URL|PGHOST|PGPORT|VITRIAL_SYNTHETIC_.*)='; then
  echo "REFUSE: inherited database/fixture configuration" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -v VITRIAL_API_ROOT ]]; then ROOT="$VITRIAL_API_ROOT"; fi
PYTHON=python3
if [[ -v VITRIAL_TEST_PYTHON ]]; then PYTHON="$VITRIAL_TEST_PYTHON"; fi
[[ -d "$ROOT/tests" && -f "$ROOT/alembic.ini" && ! -e "$ROOT/.env" ]] || {
  echo "REFUSE: API checkout invalid or .env present" >&2
  exit 2
}
for tool in docker awk mktemp git; do command -v "$tool" >/dev/null || exit 2; done
"$PYTHON" -c 'import alembic, pytest, asyncpg, sqlalchemy' >/dev/null

# Pin the currently cached image ID, not the moving postgres:17 tag.
IMAGE_ID="$(docker image inspect postgres:17 --format '{{.Id}}' 2>/dev/null)" || {
  echo "REFUSE: PostgreSQL 17 image not cached" >&2; exit 2;
}
[[ "$IMAGE_ID" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 2
NAME="vitrial-synthetic-pg-$$-$RANDOM"
PASS="$("$PYTHON" -c 'import secrets;print(secrets.token_hex(18))')"
SCRATCH="$(mktemp -d)"
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf -- "$SCRATCH"
}
trap cleanup EXIT INT TERM

docker run --rm -d --pull=never --name "$NAME" \
  -p 127.0.0.1::5432 -e POSTGRES_DB=vitrial -e POSTGRES_USER=vitrial \
  -e POSTGRES_PASSWORD="$PASS" "$IMAGE_ID" >/dev/null
PORT="$(docker port "$NAME" 5432/tcp | awk -F: '$1=="127.0.0.1" {print $2}' | head -1)"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || exit 2
for try in $(seq 1 30); do
  docker exec "$NAME" pg_isready -q -U vitrial -d vitrial && break
  sleep 1
done
docker exec "$NAME" pg_isready -q -U vitrial -d vitrial
cd "$ROOT"
export DATABASE_URL="postgresql+asyncpg://vitrial:$PASS@127.0.0.1:$PORT/vitrial"
export DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1
export EVIDENCE_STORAGE_PROVIDER=local EVIDENCE_ROOT="$SCRATCH/evidence"
"$PYTHON" -m alembic upgrade head >/dev/null
printf 'synthetic_postgres=READY image_id=%s source_sha=%s\n' \
  "$IMAGE_ID" "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
"$PYTHON" -m pytest -q tests --disable-warnings
printf 'synthetic_postgres=PASS production_deployed=false\n'