# Flat layout (package at repo root) — `incidentiq` imports with no path tricks
# from the project dir. Reverted from src/ to kill the editable-.pth import gap
# (missing-trailing-newline bug, see plans/TASK_09).
PY := .venv/bin/python

.PHONY: help test test-fast run ingest sync lint

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test:  ## Run the full test suite
	$(PY) -m pytest -q

test-fast:  ## Run only DB-free / model-free tests (skip slow integration)
	$(PY) -m pytest -q -k "not corpus and not ingestion and not durability"

run:  ## Serve the FastAPI app (ingestion + SSE) with reload
	$(PY) -m uvicorn incidentiq.api.app:app --reload --port 8000

ingest:  ## Seed the corpus into pgvector (postmortems + runbooks)
	$(PY) -m incidentiq.retrieval.init_corpus

sync:  ## Install/refresh dependencies from pyproject + uv.lock
	uv sync

lint:  ## Byte-compile everything as a fast smoke check
	$(PY) -m compileall -q incidentiq
