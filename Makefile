.PHONY: test compile validate-openapi hygiene lint ci run db-init smoke

PYTHON ?= python

all: ci

ci: test compile validate-openapi hygiene lint

test:
	CONTROL_DATABASE_URL=:memory: PYTHONPATH=apps/api $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

compile:
	$(PYTHON) -m compileall -q apps/api tests scripts

validate-openapi:
	PYTHONPATH=apps/api $(PYTHON) scripts/validate_openapi.py

hygiene:
	$(PYTHON) scripts/repository_hygiene.py

lint:
	ruff check --select F apps/api tests scripts
	ruff format --check scripts/validate_openapi.py scripts/repository_hygiene.py

db-init:
	PYTHONPATH=apps/api $(PYTHON) scripts/init_db.py

run:
	PYTHONPATH=apps/api uvicorn app.main:app --host 0.0.0.0 --port 8000

smoke:
	PYTHONPATH=apps/api $(PYTHON) scripts/live_smoke.py
