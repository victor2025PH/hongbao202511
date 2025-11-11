# Makefile for RedPacket Bot

PYTHON := python
PIP := pip

APP := app.py

.PHONY: help run test lint install freeze db-init

help:
	@echo "Available commands:"
	@echo "  make run        - Run the bot"
	@echo "  make test       - Run pytest"
	@echo "  make lint       - Run flake8 lint check"
	@echo "  make install    - Install dependencies"
	@echo "  make freeze     - Export requirements.txt"
	@echo "  make db-init    - Initialize database"

run:
	$(PYTHON) $(APP)

test:
	pytest -vv --disable-warnings

lint:
	flake8 . --max-line-length=100

install:
	$(PIP) install -r requirements.txt

freeze:
	$(PIP) freeze > requirements.txt

db-init:
	$(PYTHON) -c "from models.db import init_db; init_db(); print('✅ Database initialized')"
