# Paystack Reconciliation Core

Work in progress. See `docs/DECISIONS.md` for why things are the way they are.

## What's in here so far

- `recon.money` — `Money`, an exact amount held as a whole number of kobo.
  Parses and prints naira, takes percentages with a named rounding rule, and
  splits one payment across several invoices without losing a kobo.
- `recon.fees` — Paystack's Nigerian rates and T+1 settlement timing, in one
  place, so a rate change is a one-file edit.
- `recon.paystack.signature` — HMAC SHA-512 check over the raw request bytes.
- `recon.paystack.events` — flattens the six Paystack events we handle, and
  folds them into the books in a way that survives out-of-order delivery.
- `recon.paystack.client` — the `verify` call, which is what we actually believe.
- `recon.ingest` / `recon.app` — the webhook endpoint: signature, idempotency,
  200 straight away, work in the background.
