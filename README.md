# Paystack Reconciliation Core

Work in progress. See `docs/DECISIONS.md` for why things are the way they are.

## What's in here so far

- `recon.money` — `Money`, an exact amount held as a whole number of kobo.
  Parses and prints naira, takes percentages with a named rounding rule, and
  splits one payment across several invoices without losing a kobo.
