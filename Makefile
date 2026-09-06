SHELL := /bin/bash

.PHONY: install postgres migrate test run check

install:
	python -m pip install -e '.[dev]'

postgres:
	docker compose up -d postgres

migrate:
	alembic upgrade head

test:
	pytest -q

run:
	uvicorn app.main:app --reload

check:
	python -m compileall -q app migrations tests
	python scripts/validate_backend_contract.py
	pytest -q
