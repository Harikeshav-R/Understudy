.PHONY: all bootstrap build-images check check-int clean cluster-down cluster-up demo deploy-prod deploy-system down eval fmt help lint reset test typecheck up

all: check

help:
	@echo "Understudy Development Commands:"
	@echo "  make bootstrap    - Sync dependencies, set up pre-commit"
	@echo "  make cluster-up   - Start local k3d cluster & registry (Phase 1A)"
	@echo "  make cluster-down - Tear down local k3d cluster"
	@echo "  make build-images - Build demo services and push to local registry"
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
	uv run pytest --cov=understudy --cov=services --cov-branch --cov-fail-under=100

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy --strict src tests services

test:
	uv run pytest --cov=understudy --cov=services --cov-branch --cov-fail-under=100

check-int:
	uv run pytest -m integration tests/integration

cluster-up:
	@echo "Starting k3d cluster and local registry..."
	@k3d cluster get ust >/dev/null 2>&1 || k3d cluster create --config deploy/k3d/cluster.yaml
	@kubectl wait --for=condition=Ready nodes --all --timeout=120s
	@echo "Cluster k3d-ust and local registry on localhost:5001 ready."

cluster-down:
	@echo "Tearing down k3d cluster..."
	@k3d cluster delete ust || true

build-images:
	@echo "Building demo service images and pushing to localhost:5001..."
	docker build -t localhost:5001/edge-gateway:good -f services/edge_gateway/Dockerfile .
	docker push localhost:5001/edge-gateway:good
	docker build -t localhost:5001/auth-service:good -f services/auth_service/Dockerfile .
	docker push localhost:5001/auth-service:good
	docker build --build-arg VARIANT=good -t localhost:5001/data-service:good -f services/data_service/Dockerfile .
	docker push localhost:5001/data-service:good
	docker build --build-arg VARIANT=regression -t localhost:5001/data-service:regression -f services/data_service/Dockerfile .
	docker push localhost:5001/data-service:regression
	docker build -t localhost:5001/worker:good -f services/worker/Dockerfile .
	docker push localhost:5001/worker:good

deploy-system:
	@echo "Deploying ust-system backing infrastructure..."
	@kubectl apply -f deploy/prod/namespace.yaml
	@kubectl apply -f deploy/system/namespace.yaml
	@kubectl apply -f deploy/system/
	@kubectl wait --for=condition=Ready pod -l app=prod-postgres -n ust-prod --timeout=120s
	@kubectl wait --for=condition=Ready pods -l app.kubernetes.io/part-of=understudy -n ust-system --timeout=120s

deploy-prod: build-images
	@echo "Deploying ust-prod demo stack..."
	@kubectl apply -f deploy/prod/namespace.yaml
	@kubectl apply -f deploy/system/prod-postgres.yaml
	@kubectl apply -f deploy/prod/configmap.yaml
	@kubectl apply -f deploy/prod/auth-service.yaml
	@kubectl apply -f deploy/prod/data-service.yaml
	@kubectl apply -f deploy/prod/edge-gateway.yaml
	@kubectl apply -f deploy/prod/worker.yaml
	@kubectl wait --for=condition=Ready pod -l app=prod-postgres -n ust-prod --timeout=120s
	@kubectl wait --for=condition=Ready pods -l app.kubernetes.io/part-of=understudy -n ust-prod --timeout=120s

up: cluster-up build-images deploy-system deploy-prod
	@echo "Understudy cluster, system infrastructure, and production demo stack ready."

down: cluster-down

reset:
	@echo "make reset is implemented in Phase 6A."

demo:
	@echo "make demo is implemented in Phase 7."

eval:
	@echo "make eval is implemented in Phase 6A."

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov .mypy_cache dist build *.egg-info
