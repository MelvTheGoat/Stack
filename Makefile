.PHONY: install lint fmt types test check corpus train eval serve clean

install:
	pip install -e ".[dev]"
	pre-commit install
	install -m 755 scripts/hooks/commit-msg .git/hooks/commit-msg
	git config user.name "Mayungbo Melvyn Oluwatobi"
	git config user.email "mlvyn.t@gmail.com"

lint:
	ruff check src tests

fmt:
	ruff format src tests
	ruff check --fix src tests

types:
	mypy

test:
	pytest

check:
	./scripts/check.sh

# Rebuild the fake-but-realistic corpus in fixtures/.
corpus:
	python -m recon.corpus.generate --out fixtures --seed 20240517

# Fit the probabilistic matcher and write models/.
train:
	python -m recon.match.training --fixtures fixtures --out models

# Reproduce every number in the README.
eval:
	python -m recon.evaluation.harness --fixtures fixtures --out out

# The review queue and the daily report, at http://localhost:8000/review
serve:
	uvicorn recon.app:app --reload --port 8000

clean:
	rm -rf out .pytest_cache .mypy_cache .ruff_cache recon.db demo.db
