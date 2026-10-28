# ShiftSense developer targets (design doc 11.2)
.PHONY: setup dev web seed test reference e2e eval docker deploy

PORT ?= 8000

setup:
	uv sync
	npm ci --prefix web
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

dev:
	@trap 'kill 0' INT TERM EXIT; \
	uv run uvicorn app.main:app --reload --reload-dir app --port $(PORT) & \
	npm --prefix web run dev & \
	wait

web:
	npm --prefix web run build

seed:
	uv run python -m scripts.seed $(if $(SEED),--seed $(SEED))

test:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	LLM_MODE=mock uv run pytest

reference:
	uv run pytest reference

e2e:
	@echo "make e2e arrives in M10 (Playwright demo script)"; exit 1

eval:
	@echo "make eval arrives in M6 (evals/run.py)"; exit 1

docker:
	docker build -t shiftsense .

deploy:
	railway up
