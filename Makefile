.PHONY: help install serve demo ask eval test clean all legacy-data legacy-demo legacy-serve legacy-eval legacy-bench

PY ?= python3
CASE ?= case-gpu-assert
MODE ?= online
export PYTHONPATH := src

help:
	@echo "AILab Ops Copilot — make targets"
	@echo "  make install   Install package (editable) + dev extras"
	@echo "  make serve     Start the V2 API; MODE=online (model/key required) or replay"
	@echo "  make ask Q='...' CASE=... MODE=online|replay   V2 investigation"
	@echo "  make demo      Strict authored V2 replay, no API key; CASE=..."
	@echo "  make eval      Layered V2 evaluation; MODE=online (model/key required) or replay"
	@echo "  make test      Run the complete V2 + preserved legacy regression suite"
	@echo "  make all       test -> V2 replay demo -> V2 replay evaluation"
	@echo "  make legacy-{data,demo,serve,eval,bench}  UNSUPPORTED V1 simulations"
	@echo "                 Temporary compatibility; remove before course release (docs/legacy-v1.md)"
	@echo "  make clean     Remove generated data and caches"

install:
	$(PY) -m pip install -e ".[dev]"

serve:
	$(PY) -m ailab_ops.cli serve --mode $(MODE)

ask:
	$(PY) -m ailab_ops.cli ask "$(Q)" --case $(CASE) --mode $(MODE)

demo:
	$(PY) -m ailab_ops.cli replay --case $(CASE)

eval:
	$(PY) -m ailab_ops.cli eval --mode $(MODE)

legacy-data:
	$(PY) -m ailab_ops.cli legacy gen-data

legacy-demo:
	@echo "UNSUPPORTED V1 simulation; remove before course release; see docs/legacy-v1.md"
	$(PY) -m ailab_ops.cli legacy demo

legacy-serve:
	$(PY) -m ailab_ops.cli legacy serve

legacy-eval:
	$(PY) -m ailab_ops.cli legacy eval --limit 200 --out runs/eval_report.json

legacy-bench:
	$(PY) -m ailab_ops.cli legacy bench --concurrency 32 --requests 200 --out runs/bench_report.json

test:
	$(PY) -m pytest -q

all: test demo
	$(MAKE) eval MODE=replay

clean:
	rm -rf runs data/generated .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
