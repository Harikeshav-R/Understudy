.PHONY: all bootstrap check check-int clean demo down eval fmt help lint reset test typecheck up

all: check

help:
	@echo "Understudy Development Commands:"
	@echo "  make bootstrap    - Sync dependencies, set up pre-commit"
	@echo "  make check        - Run full validation (ruff, mypy, import-linter, 100% test cov)"
	@echo "  make fmt          - Auto-format and fix with ruff"
	@echo "  make lint         - Run ruff lint and format checks"
	@echo "  make typecheck    - Run mypy strict type checker"
	@echo "  make test         - Run unit tests with 100% coverage enforcement"
	@echo "  make check-int    - Run integration tests (requires make up)"
	@echo "  make up           - Start local cluster & stack (Phase 1+)"
	@echo "  make down         - Tear down local cluster & stack"
	@echo "  make reset        - Reset cluster state to pre-incident baseline"
	@echo "  make demo         - Run scripted demo"
	@echo "  make eval         - Run scenario evaluation suite"

bootstrap:
	uv sync --all-groups
	uv run pre-commit install

check: lint typecheck
	uv run python scripts/check_mocks.py
	uv run lint-imports
	uv run pytest --cov=understudy --cov-branch --cov-fail-under=100

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy --strict src tests

test:
	uv run pytest --cov=understudy --cov-branch --cov-fail-under=100

check-int:
	uv run pytest -m integration tests/integration

up:
	@echo "make up is implemented in Phase 1A."

down:
	@echo "make down is implemented in Phase 1A."

reset:
	@echo "make reset is implemented in Phase 6A."

demo:
	@echo "make demo is implemented in Phase 7."

eval:
	@echo "make eval is implemented in Phase 6A."

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov .mypy_cache dist build *.egg-info
