.DEFAULT_GOAL := help
UV := uv

.PHONY: help setup up down logs fmt lint types test test-arch test-unit test-int check clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Install dependencies from the lockfile
	$(UV) sync --group dev

up:  ## Start PostgreSQL, Redis, MinIO
	docker compose up -d --wait

down:  ## Stop the containers
	docker compose down

logs:  ## Tail container logs
	docker compose logs -f

fmt:  ## Format
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

lint:  ## Lint
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

types:  ## Type-check
	$(UV) run mypy

test-arch:  ## Trust-boundary enforcement only (ADR-0004 / DoD-9)
	$(UV) run pytest tests/architecture -v

test-unit:  ## Unit tests, no containers required
	$(UV) run pytest -m "not integration"

test-int:  ## Integration tests, requires `make up`
	$(UV) run pytest -m integration

test:  ## Full test suite
	$(UV) run pytest

check: lint types test  ## Everything CI runs

clean:  ## Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
