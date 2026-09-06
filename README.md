# Vitrial Connected Operations API

Dedicated backend for the Vitrial 0.2.0 Connected Operations client. This service intentionally lives outside the iOS application repository.

## Contract boundary

The initial V1 compatibility boundary is the portable contract pack from `scottjoyner/vitrialuminios-del-valle-ios` at exact client SHA:

`74822ac9e2139ac35ea4fbb35ae3323e6a2745c6`

Backend code must remain compatible with `contracts/backend/v1/` and `scripts/validate_backend_contract.py` from that client checkpoint.

## Stack

- FastAPI
- PostgreSQL
- SQLAlchemy 2.x async
- Alembic
- Pydantic Settings
- pytest
- Docker / Compose

## First run

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload
```

## V1 endpoints

- `GET /health`
- `GET /api/v1/version`
- `GET /api/v1/auth/me`
- `POST /api/v1/sync/push`
- `GET /api/v1/sync/pull`
- `HEAD /api/v1/sync/evidence-blobs/{documentID}`
- `PUT /api/v1/sync/evidence-blobs/{documentID}`
- `GET /api/v1/sync/evidence-blobs/{documentID}`

## Security invariants

- Access tokens are stored only as SHA-256 hashes.
- Roles are informational; explicit capabilities are authority.
- Server mutations must resolve canonical tenant/parent ownership from server state.
- `clientMutationID` is idempotent within an organization.
- Stale `baseServerRevision` must never silently overwrite canonical state.
- Audit/history provenance stores actor/org/membership/session/revision, never bearer tokens.
- Evidence uploads are digest checked before canonical promotion.

## Current status

This repository starts as a backend bootstrap, not a production-complete 0.2.0 release. Authorization-by-canonical-ownership, complete integration coverage, object-storage abstraction, structured correlation logging, and two-user/two-device acceptance remain release gates.
