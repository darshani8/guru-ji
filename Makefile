.PHONY: test compile run db-init smoke

PYTHON ?= python

all: test compile

test:
	PYTHONPATH=apps/api $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

compile:
	$(PYTHON) -m compileall -q apps/api tests

db-init:
	PYTHONPATH=apps/api $(PYTHON) scripts/init_db.py

run:
	PYTHONPATH=apps/api uvicorn app.main:app --host 0.0.0.0 --port 8000

smoke:
	PYTHONPATH=apps/api $(PYTHON) scripts/live_smoke.py
