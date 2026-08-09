.PHONY: up down logs restart build test lint clean ps

COMPOSE_FILE = infrastructure/docker-compose.yml

## Start all services (Docker Compose)
up:
	docker compose -f $(COMPOSE_FILE) up -d

## Stop all services
down:
	docker compose -f $(COMPOSE_FILE) down

## Follow logs of all services
logs:
	docker compose -f $(COMPOSE_FILE) logs -f

## Restart all services
restart: down up

## Rebuild images (after Dockerfile/requirements changes)
build:
	docker compose -f $(COMPOSE_FILE) build

## Show running services
ps:
	docker compose -f $(COMPOSE_FILE) ps

## Run backend unit tests locally (outside Docker)
# --continue-on-collection-errors: one broken test file (e.g. a missing
# optional dependency) must not silently abort every other suite before it
# even runs -- --maxfail=1 was doing exactly that, since a collection
# error counts as an immediate failure. Note this does NOT cover a broken
# conftest.py specifically (pytest treats that as fatal regardless); a
# missing dependency behind a package's own __init__.py (e.g. src/lib/rag)
# can still take down that one directory's conftest.
test:
	pytest --continue-on-collection-errors --disable-warnings -q

## Lint + type-check the Python codebase
lint:
	ruff check src/
	mypy src/

## Remove volumes too (WARNING: wipes local Postgres/Qdrant data)
clean:
	docker compose -f $(COMPOSE_FILE) down -v
