.PHONY: help install run test lint decks bots docs-pdf up down logs

help:
	@echo "install   — поставить зависимости (dev)"
	@echo "run       — поднять сервер локально с автоперезагрузкой"
	@echo "test      — прогнать тесты (pytest, с фолбэком на свой раннер)"
	@echo "lint      — ruff"
	@echo "decks     — перегенерировать data/decks.json (COUNT=10)"
	@echo "bots      — посадить ботов за стол (BOTS=6 HANDS=10)"
	@echo "docs-pdf  — пересобрать PDF-версии документации"
	@echo "up/down   — docker compose"

install:
	pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	@pytest 2>/dev/null || python tests/run_all.py

lint:
	ruff check app scripts tests

COUNT ?= 10
decks:
	python scripts/generate_decks.py --count $(COUNT)

BOTS ?= 6
HANDS ?= 10
bots:
	python3 scripts/bot_client.py --bots $(BOTS) --hands $(HANDS)

docs-pdf:
	python scripts/build_docs_pdf.py

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api
