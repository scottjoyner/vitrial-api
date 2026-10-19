#!/usr/bin/env bash
# SCRUM-49/64: disposable actual-HTTP two-user canary, never production.
set -euo pipefail
if env | grep -Eq '^(DATABASE_URL|PGHOST|PGPORT|VITRIAL_ADMIN_KEY|ADMIN_API_KEY_HASH|VITRIAL_SYNTHETIC_.*)='; then
  echo "REFUSE inherited database/admin/fixture configuration" >&2; exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -v VITRIAL_API_ROOT ]]; then ROOT="$VITRIAL_API_ROOT"; fi
PYTHON=python3
if [[ -v VITRIAL_TEST_PYTHON ]]; then PYTHON="$VITRIAL_TEST_PYTHON"; fi
[[ -f "$ROOT/alembic.ini" && -f "$ROOT/scripts/bootstrap_two_user_acceptance.py" &&
   -d "$ROOT/app" && ! -e "$ROOT/.env" ]] || {
  echo "REFUSE missing API source or checkout .env" >&2; exit 2;
}
"$PYTHON" -c 'import uvicorn, alembic, asyncpg, sqlalchemy' >/dev/null
IMAGE_ID="$(docker image inspect postgres:17 --format '{{.Id}}' 2>/dev/null)" || exit 2
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
NAME="vitrial-canary-pg-$$-$RANDOM"
SCRATCH="$(mktemp -d)"
chmod 700 "$SCRATCH"
API_PID=""
cleanup() {
  if [[ -n "$API_PID" ]]; then kill "$API_PID" >/dev/null 2>&1 || true; wait "$API_PID" 2>/dev/null || true; fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf -- "$SCRATCH"
}
trap cleanup EXIT INT TERM
PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(18))')"
ADMIN_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
ADMIN_HASH="$(ADMIN_KEY="$ADMIN_KEY" "$PYTHON" -c 'import os,hashlib; print(hashlib.sha256(os.environ["ADMIN_KEY"].encode()).hexdigest())')"
docker run -d --rm --pull=never --name "$NAME" -p 127.0.0.1::5432 \
  -e POSTGRES_DB=vitrial -e POSTGRES_USER=vitrial -e POSTGRES_PASSWORD="$PASSWORD" \
  "$IMAGE_ID" >/dev/null
PORT="$(docker port "$NAME" 5432/tcp | awk -F: '$1=="127.0.0.1" {print $2}' | head -1)"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || exit 2
for i in $(seq 1 35); do docker exec "$NAME" pg_isready -q -U vitrial -d vitrial && break || sleep 1; done
docker exec "$NAME" pg_isready -q -U vitrial -d vitrial
cd "$ROOT"
export DATABASE_URL="postgresql+asyncpg://vitrial:$PASSWORD@127.0.0.1:$PORT/vitrial"
export DATABASE_NULL_POOL=true EVIDENCE_STORAGE_PROVIDER=local
export EVIDENCE_ROOT="$SCRATCH/evidence" ADMIN_API_KEY_HASH="$ADMIN_HASH"
export VITRIAL_ADMIN_KEY="$ADMIN_KEY"
"$PYTHON" -m alembic upgrade head >"$SCRATCH/migration.log" 2>&1

# Python reserves a random loopback port briefly. The probe checks identity
# before writing fixtures, preventing silent contact with a wrong server.
HTTP_PORT="$("$PYTHON" -c 'import socket; s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$HTTP_PORT" \
  >"$SCRATCH/uvicorn.log" 2>&1 &
API_PID=$!
API_ORIGIN="http://127.0.0.1:$HTTP_PORT"
for i in $(seq 1 35); do
  if "$PYTHON" -c 'import sys,urllib.request; u=sys.argv[1]; r=urllib.request.urlopen(u+"/ready", timeout=1); assert r.status==200' "$API_ORIGIN" 2>/dev/null; then break; fi
  kill -0 "$API_PID" 2>/dev/null || {
    echo "API exited early (sanitized startup diagnostics)" >&2
    grep -E "^(ERROR:|INFO:|[A-Za-z0-9_.]+(Error|Exception):)" "$SCRATCH/uvicorn.log" | tail -6 >&2 || true
    exit 1
  }
  sleep 1
done
"$PYTHON" -c 'import sys,json,urllib.request; b=sys.argv[1]; r=urllib.request.urlopen(b+"/ready",timeout=4); j=json.load(r); assert r.status==200 and j["status"]=="ready" and j["database"]=="ok" and j["objectStorage"]=="ok"' "$API_ORIGIN"
"$PYTHON" scripts/bootstrap_two_user_acceptance.py \
  --base-url "$API_ORIGIN" --allow-http-localhost \
  --output "$SCRATCH/handoff.json" >"$SCRATCH/bootstrap.log" 2>&1
"$PYTHON" -c 'import json,os,sys; p=sys.argv[1]; d=json.load(open(p)); assert len(d["users"])==2; assert os.stat(p).st_mode & 0o777 == 0o600' "$SCRATCH/handoff.json"
"$PYTHON" "$SCRIPT_DIR/verify_disposable_third_tenant.py" \
  --base-url "$API_ORIGIN" --handoff "$SCRATCH/handoff.json"
printf 'HTTP_CANARY_PASS source_sha=%s postgres_image=%s\n' \
  "$(git rev-parse HEAD)" "$IMAGE_ID"
printf 'ready=200 two_distinct_users=PASS shared_customer_project_sync=PASS production_changed=false\n'