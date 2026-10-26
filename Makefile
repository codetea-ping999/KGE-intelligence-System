# All targets run offline once `make setup` has installed the locked dependencies.
UV ?= uv
RUN = $(UV) run --frozen
export KGE_CLOCK ?= system

.PHONY: setup test lint typecheck check demo qa repro clean

setup:
	$(UV) sync --frozen

test:
	$(RUN) pytest -q

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

typecheck:
	$(RUN) mypy src

check: lint typecheck test

demo:
	$(RUN) kge demo --db build/demo.db

qa: demo
	$(RUN) kge --db build/demo.db qa

# Runs the reference scenario twice and requires byte-identical output.
repro:
	@mkdir -p build
	$(RUN) kge demo --db :memory: > build/demo_run1.txt
	$(RUN) kge demo --db :memory: > build/demo_run2.txt
	diff build/demo_run1.txt build/demo_run2.txt && echo "reproducible: outputs are identical"

clean:
	rm -rf build .pytest_cache .mypy_cache .ruff_cache
