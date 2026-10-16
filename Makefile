.PHONY: install lint format test eval up down

install:
	pip install -e ".[dev,otlp]"

lint:
	ruff check src tests examples
	ruff format --check src tests examples

format:
	ruff check --fix src tests examples
	ruff format src tests examples

test:
	pytest --cov=tracegate --cov-report=term-missing

eval:
	tracegate eval --dataset datasets/golden.jsonl --out reports

up:
	docker compose up -d

down:
	docker compose down
