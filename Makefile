.PHONY: install demo test lint fmt lock audit

install:
	pip install --require-hashes -r requirements-dev.txt

# The gate on deterministic stubs: a clean run, a regression, a budget breach.
demo:
	-python -m evalgate --golden examples/golden.yaml --prices examples/prices.yaml --offline --current stub/baseline --candidate stub/regressed

test:
	pytest -q

lint:
	ruff check evalgate tests && ruff format --check evalgate tests && mypy evalgate

fmt:
	ruff format evalgate tests && ruff check --fix evalgate tests

PIP_COMPILE = pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --no-emit-index-url
lock:
	$(PIP_COMPILE) $(ARGS) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(ARGS) -o requirements-dev.txt requirements-dev.in

audit:
	pip-audit -r requirements.txt --no-deps --disable-pip --progress-spinner off
