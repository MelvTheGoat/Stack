# Working agreement

## Git
- Commit after each logically complete unit of work. Never batch a phase into one commit.
- Conventional commits: feat:, fix:, test:, refactor:, docs:, chore:, perf:
- Commit message body explains *why*, not what. One or two lines.
- Never add any AI attribution, co-author trailer, or generated-with footer to a
  commit message, PR description, branch name, or code comment.
- Branch names: feat/<short-slug>, fix/<short-slug>. No other prefixes.
- Never force-push. Never rewrite history. Never squash on merge.
- Do not run `git commit` with a heredoc containing trailers.

## Engineering
- Every module ships with its tests in the same commit.
- No feature is "done" until it has a test and a line in the README.
- Fail loudly on money errors. No silent excepts around amount handling.
- All monetary values are integers in kobo. Never floats. Ever.
- Type hints everywhere; mypy and ruff must pass before any commit.
- If a design decision has a real trade-off, write it into docs/DECISIONS.md
  with the rejected alternative.

## Writing
- Explain things the way you would to a smart kid. Short sentences. Real examples.
- Say the honest number, not the flattering one.
