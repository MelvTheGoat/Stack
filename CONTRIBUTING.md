# Contributing

## Git

- Commit after each logically complete unit of work. Never batch a phase into
  one commit.
- Conventional commits: `feat:`, `fix:`, `test:`, `refactor:`, `docs:`,
  `chore:`, `perf:`.
- The body explains *why*, not what. One or two lines.
- Commit messages carry no trailers. A `commit-msg` hook strips them, installed
  by `make install`.
- Branch names: `feat/<short-slug>`, `fix/<short-slug>`. No other prefixes.
- Never force-push. Never rewrite history. Never squash on merge.
- When something breaks, commit the fix separately from the feature, with a
  message saying what broke. The history is the engineering log.

## Engineering

- Every module ships with its tests in the same commit.
- Nothing is done until it has a test and a line in the README.
- Fail loudly on money errors. No silent `except` around amount handling.
- All monetary values are integers in kobo. Never floats. Ever.
- Type hints everywhere. `./scripts/check.sh` must pass before any commit:
  ruff, ruff format, mypy, pytest. Run it, do not pipe it through `tail` —
  that hides the exit code.
- If a design decision has a real trade-off, write it into `docs/DECISIONS.md`
  with the rejected alternative and what the choice costs.

## Numbers

- No number goes in the README that `make eval` cannot regenerate.
  `tests/test_readme.py` enforces this.
- Say the honest number, not the flattering one. If a measurement is weak —
  small sample, tuned on its own test set — say so next to it.

## Writing

- Explain things the way you would to a smart kid. Short sentences. Real
  examples.
- Comments say why, not what. If a line needs a comment to explain what it
  does, rewrite the line.
