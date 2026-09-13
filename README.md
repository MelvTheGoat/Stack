# Paystack Reconciliation Core

Work in progress. See `docs/DECISIONS.md` for why things are the way they are.

## What's in here so far

- `recon.money` — `Money`, an exact amount held as a whole number of kobo.
  Parses and prints naira, takes percentages with a named rounding rule, and
  splits one payment across several invoices without losing a kobo.
- `recon.corpus` — generates a month of realistic payments plus the answer key
  that says what each one really was. `fixtures/` is the committed output.
- `recon.match.ledger` — who owes what, indexed for lookup.
- `recon.match.deterministic` — layer one: exact reference, dedicated-account
  attribution, exact amount in a time window, and a duplicate guard. Closes
  **69.9%** of the corpus and has never yet been wrong on the ones it answers.
- `recon.match.similarity` — name matching that survives reordering, spelling
  variants and dropped letters, without merging two different customers.
- `recon.match.candidates` — shortlisting, including sets of invoices that add
  up to exactly what was paid.
- `recon.match.features` / `recon.match.model` — eighteen named features and a
  logistic fit, written out longhand so the weights can be read.
- `recon.match.calibration` — Platt and isotonic, chosen between on held-out
  data, with a reliability diagram and a Brier score.
- `recon.match.threshold` — the cost matrix, in naira, with its assumptions
  stated.
- `recon.match.pipeline` — deterministic first, probabilistic second, and a
  report of which layer resolved what.
- `recon.evaluation.truth` / `recon.evaluation.score` — reads the answer key and
  turns matches into numbers.
- `recon.fees` — Paystack's Nigerian rates and T+1 settlement timing, in one
  place, so a rate change is a one-file edit.
- `recon.paystack.signature` — HMAC SHA-512 check over the raw request bytes.
- `recon.paystack.events` — flattens the six Paystack events we handle, and
  folds them into the books in a way that survives out-of-order delivery.
- `recon.paystack.client` — the `verify` call, which is what we actually believe.
- `recon.ingest` / `recon.app` — the webhook endpoint: signature, idempotency,
  200 straight away, work in the background.
