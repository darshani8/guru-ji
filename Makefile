.PHONY: test compile validate-openapi hygiene lint ci run db-init smoke postgres-smoke connector-smoke all-phases worker monitor platform-smoke platform-postgres-smoke openapi

PYTHON ?= python
PYTHONPATH_VALUE = apps/api:apps

all: ci

ci: test compile validate-openapi hygiene lint

test:
	CONTROL_DATABASE_URL=:memory: PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

compile:
	$(PYTHON) -m compileall -q apps/api apps/connector tests scripts

validate-openapi:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) scripts/validate_openapi.py

hygiene:
	$(PYTHON) scripts/repository_hygiene.py

lint:
	ruff check --select F apps/api apps/connector tests scripts
	ruff format --check scripts/validate_openapi.py scripts/repository_hygiene.py

db-init:
	PYTHONPATH=apps/api $(PYTHON) scripts/init_db.py

run:
	PYTHONPATH=apps/api uvicorn app.main:app --host 0.0.0.0 --port 8000

smoke:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) scripts/live_smoke.py

platform-smoke:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) scripts/platform_smoke.py

worker:
	PYTHONPATH=apps/api $(PYTHON) scripts/run_worker.py

monitor:
	PYTHONPATH=apps/api $(PYTHON) scripts/run_monitor.py

openapi:
	PYTHONPATH=$(PYTHONPATH_VALUE) CONTROL_DATABASE_URL=:memory: $(PYTHON) scripts/export_openapi.py

postgres-smoke:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) scripts/postgres_smoke.py

platform-postgres-smoke:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) scripts/platform_postgres_smoke.py

connector-smoke:
	PYTHONPATH=$(PYTHONPATH_VALUE) $(PYTHON) -m pytest -q tests/test_all_phase_completion.py

all-phases: ci connector-smoke
