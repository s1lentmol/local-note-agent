UV ?= uv
COMPOSE ?= docker compose
# Existing SQLite/ORM tests require the prototype extra; CI runs them separately.
PROTOTYPE_TESTS := tests/test_migrations.py tests/test_mvp.py tests/test_notes.py tests/test_web.py tests/test_worker_process.py

.PHONY: setup up down logs lint test migrate test-db
setup:
	$(UV) sync --project backend --locked --python 3.11
up:
	$(COMPOSE) up --build --wait
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs --follow
lint:
	$(UV) run --directory backend --locked ruff check .
	$(UV) run --directory backend --locked ruff format --check .
test:
	$(UV) run --directory backend --locked pytest tests $(addprefix --ignore=,$(PROTOTYPE_TESTS)) --ignore=tests/postgres

# Migrations are an explicit deployment step; API startup never creates schema.
migrate:
	$(COMPOSE) run --rm --no-deps api python migrate.py
test-db:
	sh scripts/test-postgres.sh
