VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv install lint test datagen bootstrap simulate detect heal eval demo clean

venv:
	python3 -m venv $(VENV)

install: venv
	$(PIP) install -q -e ".[dev]"

lint:
	$(VENV)/bin/ruff check src tests data_gen

test:
	$(VENV)/bin/pytest -q

datagen:
	$(PY) data_gen/generate_banking_data.py

# --- workspace targets (need PAT in Keychain: service databricks-fe) ---------

bootstrap:  ## create schema/tables, load data, create metric views + Genie space
	$(PY) -m genie_autopilot.cli bootstrap

simulate:   ## run the synthetic persona fleet against the live Genie space
	$(PY) -m genie_autopilot.cli simulate

detect:     ## harvest telemetry and print scored drift proposals
	$(PY) -m genie_autopilot.cli detect

heal:       ## apply approved proposals (governed gate) and log to the audit ledger
	$(PY) -m genie_autopilot.cli heal

eval:       ## run the Genie benchmark suite and print the accuracy scorecard
	$(PY) -m genie_autopilot.cli eval

demo: bootstrap eval simulate detect heal eval  ## full before/after flywheel demo

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache data_gen/output
