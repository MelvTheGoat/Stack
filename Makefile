.PHONY: install lint fmt types test check corpus eval serve review clean

install:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check src tests

fmt:
	ruff format src tests
	ruff check --fix src tests

types:
	mypy

test:
	pytest

check: lint types test

# Rebuild the fake-but-realistic corpus in fixtures/.
corpus:
	python -m recon.corpus.generate --out fixtures --seed 20240517

# Reproduce every number in the README.
eval:
	python -m recon.evaluation.harness --fixtures fixtures --out out

serve:
	uvicorn recon.app:app --reload --port 8000

clean:
	rm -rf out .pytest_cache .mypy_cache .ruff_cache recon.db
