# ShiftSense developer targets (design doc 11.2)
.PHONY: setup dev web api seed test reference e2e eval docker deploy

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

api:  # regenerate web/openapi.json and the TypeScript client types from the FastAPI schema
	uv run python -m scripts.export_openapi
	npm --prefix web run gen:api

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

# live intake eval against Baseten (spends credits, at most 3 full runs a day, 9.4);
# EVAL_ARGS="--mode replay" re-runs it from data/llm_cache.jsonl for free
eval:
	uv run python -m evals.run --suite intake --provider baseten $(EVAL_ARGS)

docker:
	docker build -t shiftsense .

deploy:
	railway up
