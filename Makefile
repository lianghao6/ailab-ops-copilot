.PHONY: help install data serve demo ask eval bench test lint clean all

PY ?= python3
export PYTHONPATH := src

help:
	@echo "AILab Ops Copilot — make targets"
	@echo "  make install   Install package (editable) + dev extras"
	@echo "  make data      Generate the synthetic dataset (deterministic)"
	@echo "  make serve     Start the API server (FastAPI + SSE)"
	@echo "  make ask Q='...'   One-shot question against the copilot API"
	@echo "  make demo      Offline end-to-end demo, no server needed"
	@echo "  make eval      Run the golden-set evaluation against ground truth"
	@echo "  make bench     Run the concurrency / load benchmark"
	@echo "  make test      Run unit tests"
	@echo "  make all       data -> eval -> bench"
	@echo "  make clean     Remove generated data and caches"

install:
	$(PY) -m pip install -e ".[dev]"

data:
	$(PY) -m ailab_ops.cli gen-data

serve:
	$(PY) -m ailab_ops.cli serve

ask:
	$(PY) -m ailab_ops.cli ask "$(Q)"

demo:
	$(PY) -m ailab_ops.cli demo

eval:
	$(PY) -m ailab_ops.cli eval --limit 200 --out runs/eval_report.json

bench:
	$(PY) -m ailab_ops.cli bench --concurrency 32 --requests 200 --out runs/bench_report.json

test:
	$(PY) -m pytest -q

all: data eval bench

clean:
	rm -rf runs data/generated .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
