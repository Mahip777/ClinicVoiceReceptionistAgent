.PHONY: install run seed test lint eval docker-up docker-down

install:
	python -m pip install -e ".[dev]"

run:
	uvicorn clinic_voice.main:app --reload

seed:
	clinic-seed --reset

test:
	pytest

lint:
	ruff check .

eval:
	clinic-eval evals/sample_calls.json

docker-up:
	docker compose up --build

docker-down:
	docker compose down

