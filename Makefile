.DEFAULT_GOAL := help
.PHONY: help install dev lint format type test run up down migrate revision load-excel

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime deps
	pip install -r requirements.txt

dev: ## Install dev + runtime deps and pre-commit hooks
	pip install -r requirements-dev.txt && pre-commit install

lint: ## Lint with ruff
	ruff check app tests

format: ## Auto-format with ruff
	ruff format app tests && ruff check --fix app tests

type: ## Static type-check with mypy
	mypy app

test: ## Run the test suite with coverage
	pytest

run: ## Run the API locally (reload)
	uvicorn app.main:app --reload

up: ## Start the local stack (db + redis + api)
	docker compose up --build

down: ## Stop the local stack
	docker compose down

migrate: ## Apply DB migrations
	alembic upgrade head

revision: ## Autogenerate a migration (m="message")
	alembic revision --autogenerate -m "$(m)"

load-excel: ## Load an Excel file (f=path t=table)
	python scripts/load_excel.py --file "$(f)" --table "$(t)"
