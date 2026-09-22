# Local development. The ordering in `setup` matters: the application role must
# exist before migrations run, because the migration grants privileges to it.

SHELL := /bin/bash
PGDATA ?= .pgdata
PGPORT ?= 55432
PGURL  := postgresql://postgres@127.0.0.1:$(PGPORT)/clinic
# The isolation tests TRUNCATE tenant tables, so they must never point at the
# development database. A separate database costs nothing and means `make test`
# can never destroy demo data.
PGTESTURL := postgresql://postgres@127.0.0.1:$(PGPORT)/clinic_test
PGTESTAPP := postgresql://clinic_app:devpass@127.0.0.1:$(PGPORT)/clinic_test
# macOS Homebrew Postgres refuses to start unless the locale is pinned, but
# exporting LC_ALL globally makes libpq hand psycopg bytes it can't parse.
# Scope it to the cluster commands only.
PGLOCALE := LC_ALL=C LANG=C

.PHONY: help db-start db-stop db-reset setup test-db migrate seed dev api web test lint fmt build

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

db-start: ## Start a local Postgres cluster on port 55432
	@test -d $(PGDATA) || $(PGLOCALE) initdb -D $(PGDATA) -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
	@$(PGLOCALE) pg_ctl -D $(PGDATA) -o "-p $(PGPORT) -c unix_socket_directories= -c listen_addresses=127.0.0.1" \
		-l $(PGDATA)/server.log start >/dev/null 2>&1 || true
	@until pg_isready -h 127.0.0.1 -p $(PGPORT) -q; do sleep 0.3; done
	@psql -q "$(PGURL)" -c '' 2>/dev/null || createdb -h 127.0.0.1 -p $(PGPORT) -U postgres clinic
	@echo "postgres ready on $(PGPORT)"

db-stop: ## Stop the local cluster
	@$(PGLOCALE) pg_ctl -D $(PGDATA) stop >/dev/null 2>&1 || true

db-reset: db-stop ## Delete the local cluster entirely
	@rm -rf $(PGDATA)

setup: db-start ## Create roles, migrate and seed from scratch
	@psql -q "$(PGURL)" -v app_password=devpass -f backend/scripts/bootstrap_roles.sql >/dev/null
	@cd backend && .venv/bin/alembic upgrade head
	@cd backend && .venv/bin/python -m scripts.seed

migrate: ## Apply migrations
	@cd backend && .venv/bin/alembic upgrade head

seed: ## Seed the demo clinic
	@cd backend && .venv/bin/python -m scripts.seed

api: ## Run the API with reload
	@cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000

web: ## Run the Vite dev server (proxies /api to :8000)
	@cd frontend && npm run dev

test-db: db-start ## Create and migrate the throwaway test database
	@psql -q "$(PGURL)" -tAc "SELECT 1 FROM pg_database WHERE datname='clinic_test'" | grep -q 1 \
		|| createdb -h 127.0.0.1 -p $(PGPORT) -U postgres clinic_test
	@psql -q "$(PGTESTURL)" -v app_password=devpass -f backend/scripts/bootstrap_roles.sql >/dev/null
	@cd backend && MIGRATION_DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:$(PGPORT)/clinic_test" \
		.venv/bin/alembic upgrade head >/dev/null 2>&1

test: test-db ## Run the full suite, including the database isolation tests
	@cd backend && TEST_DATABASE_URL="$(PGTESTURL)" TEST_APP_DATABASE_URL="$(PGTESTAPP)" \
		.venv/bin/python -m pytest -q

lint: ## Lint and format check
	@cd backend && .venv/bin/ruff check app scripts tests && .venv/bin/ruff format --check app scripts tests

fmt: ## Auto-format
	@cd backend && .venv/bin/ruff check --fix app scripts tests && .venv/bin/ruff format app scripts tests

build: ## Build the production image
	@docker build -t clinic-chat-bot:local .
