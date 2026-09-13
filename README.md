# Paystack Reconciliation Core

A Nigerian shop takes money four different ways and has to match every payment
to an invoice before it closes its books. This does most of that matching on its
own, and hands a person the cases where it is not sure, ranked so the biggest
money gets looked at first.

Live instance: _not deployed yet — `docs/DEPLOY.md` has the Cloud Run steps, and
this line gets a URL when it goes up._

---

## The problem, in plain terms

Money arrives four ways:

| How | What we know about it |
| --- | --- |
| **Card** | Everything. The customer paid through our own checkout, so our invoice number came along with it. |
| **Dedicated account** | Whose money it is. Each customer has their own account number, so anything landing there is theirs. We still do not know *which* invoice. |
| **Bank transfer** | A line of text. Something like `NIP/GTB/OKONKWO ADA/PAYMENT`. That is it. |
| **Cash** | Whatever the person at the counter typed. Nothing verifies it. |

Separately there is a list of invoices. Every evening somebody sits down and
matches the two lists by hand, and it goes wrong in the same ways every time:

- the name on the transfer is spelled differently from the name on the invoice,
  or reversed, or shortened
- somebody paid part of an invoice
- somebody paid a bit too much
- one transfer covers three invoices
- the same payment got entered twice
- money came in from someone who owes nothing
- two invoices are for the same amount and either one fits
- the bank credit is a day late and smaller than the sales, because of fees

---

## How it works

One rule runs the whole thing:

> **Certain things first. Scored things second. A model last.**

Four exact rules run first, and none of them is allowed to fire when two
invoices fit — a tie is not a certainty. Whatever is left gets scored by a small
model, and only the scores above a line drawn from real naira costs get closed
without a person. Everything else goes to a queue.

```
payment in
    │
    ├─ is this the same money we already saw an hour ago?         → a person decides
    ├─ is our invoice number written on it?                       → done, certain
    ├─ did it land in one customer's own account number,
    │    and do they have exactly one invoice for this amount?     → done, certain
    ├─ is there exactly one unpaid invoice for this exact
    │    amount, from the last few days?                           → done, certain
    │
    ├─ score every invoice it could plausibly be, using the name,
    │    the amount, the timing, the channel and who has paid
    │    for this customer before                                  → done, if we are sure enough
    │
    └─ otherwise                                                   → the review queue
```

Every decision records which layer made it, so the mix can be reported for any
day rather than guessed at.

---

## The numbers

All of these come out of `make eval`, which rebuilds the corpus, refits the
model and re-scores everything from scratch. Nothing here is typed in by hand.

**439 payments over one month, worth ₦37,181,750.67.**

| Layer | Resolved | Share | Right | Money |
| --- | ---: | ---: | ---: | ---: |
| exact reference | 164 | 37.4% | 100.0% | ₦13,229,863.68 |
| dedicated account | 25 | 5.7% | 100.0% | ₦2,531,547.25 |
| amount + time window | 118 | 26.9% | 100.0% | ₦7,884,224.74 |
| scored by the model | 27 | 6.2% | 100.0% | ₦3,761,274.75 |
| **sent to a person** | **105** | **23.9%** | — | ₦9,774,840.25 |

- **Closed without a person: 76.1%.** Rules did 69.9% of it, the model 6.2%.
- **Precision: 100%.** Nothing was closed wrongly, on this corpus.
- **Recall: 80.1%.** Of the payments that genuinely do pay an invoice, four in
  five were found and closed.
- **Nothing dangerous was closed unattended.** Not one duplicate, and not one
  payment from a stranger.

### Where the remaining quarter goes

The 105 that reach a person are not random: they are the part payments, the
overpayments, the split payments, the duplicates and the money from nowhere.
That is the correct answer for those, not a failure.

| Case | In the month | Closed alone | Sent to a person |
| --- | ---: | ---: | ---: |
| card, our reference attached | 115 | 115 | 0 |
| transfer, name only, exact amount | 100 | 100 | 0 |
| **transfer, underpaid** | 46 | 2 | 44 |
| cash | 36 | 36 | 0 |
| transfer, reference in the narration | 34 | 34 | 0 |
| dedicated account, exact amount | 25 | 25 | 0 |
| **transfer, overpaid** | 22 | 2 | 20 |
| **one transfer, three invoices** | 17 | 7 | 10 |
| **dedicated account, part payment** | 12 | 3 | 9 |
| **the same payment sent twice** | 12 | 0 | 12 |
| two identical invoices, either fits | 10 | 10 | 0 |
| **money that pays no invoice at all** | 10 | 0 | 10 |

### Does the confidence mean anything

A model that says "90%" and is right half the time is worse than useless here,
because the whole design rests on "close anything above the line". So the scores
are calibrated and then checked on 390 candidate comparisons the model never
trained on:

- **Brier score: 0.0219.** (0 is perfect. 0.25 is what you get by saying "50%"
  to everything.)
- **Expected calibration error: 0.024.** On average a stated probability is
  about two and a half points away from the truth.
- Platt scaling and isotonic regression were both fitted and compared;
  Platt won. Both beat leaving the raw scores alone (0.093 → 0.052).

### Where the line was drawn, and why

The threshold is **0.85**, and it was not chosen by eye. It comes from what the
two possible mistakes actually cost:

| | Cost | Reasoning |
| --- | ---: | --- |
| Checking a match that was fine anyway | **₦60** | 3 minutes of a bookkeeper's time at ₦1,200/hour |
| Closing a match that was wrong | **₦5,000** | 2 hours to notice it, trace it and reverse it, plus ₦2,600 for a customer who was chased for money they had already paid |

That is **83 to 1**. At that ratio you have to be wrong less than about 1.2% of
the time for closing a case unattended to be worth it, which is why the line
sits high and the model only closes 6% of the month. Six percent that is right
beats twenty percent that is mostly right. Change those two numbers in
`src/recon/match/threshold.py` and re-run `make eval`; the line moves on its own.

### The evening

A person gets through about forty of these after closing. Sorted by money at
risk, **the first forty cover 91% of the money** — ₦8,901,126 of ₦9,774,840.
Eighty-nine of the 105 arrive with a suggestion attached and the reasons for it
written out in words.

### Against doing it by hand

| | Hours | Cost |
| --- | ---: | ---: |
| A person matching all 439 payments | 21.9 | ₦26,340 |
| A person reviewing the 105 this leaves | 5.2 | ₦6,300 |
| **Saved** | **16.7** | **₦20,040** |

This assumes the person doing it by hand never makes a mistake, which they
would. So the saving is the pessimistic end.

### Reading reports written by people

The system also takes payment reports in prose — "Ada Okonkwo paid 45k for
invoice 42 this morning by transfer" — and turns them into typed records.
Measured separately from matching, because they fail for different reasons.

On 40 hand-labelled reports: **94.4% read completely right, 100% of the
unreadable ones correctly refused, and nothing invented.** Every amount it read
was right.

Read that number carefully. Forty examples, written by the same person who wrote
the parser. Ten of them were written afterwards and never used to tune it, and
those ten immediately found four real bugs. The honest description is "it has
not failed on these yet", not an accuracy rate.

---

## What it does not do

Not "future work could include". These are things that are wrong with it today.

**Cash cannot be verified, and nothing can fix that.** A cash payment exists
because a person typed it in. There is no Paystack record, no bank record, no
signature. The system marks those rows attested rather than verified and takes
the human's word for it. If somebody pockets ₦20,000 and records ₦15,000, this
will reconcile cheerfully and be wrong. That is not a bug to be fixed later; it
is the limit of what any software can do with cash.

**The numbers describe a simulation.** The corpus is generated, not real. How
often people type the invoice number in, how often a bank reverses the name —
those are guesses. They are written out in one place (`SCENARIOS` in
`src/recon/corpus/generate.py`) so you can see what was assumed and disagree,
but if the guesses are wrong then every percentage above moves with them. Real
data would be better and is not ours to publish.

**The threshold was chosen on 87 payments.** Cross-validation over the residual
is the honest way to use a corpus this small, and it is still 87 payments. At an
83-to-1 cost ratio you would want several hundred before trusting a line to two
decimal places.

**100% precision will not survive contact with real data.** Zero wrong
auto-clears on 334 closed cases is a real number on this corpus and an
unrealistic expectation anywhere else. Watch the rejection reasons in the review
queue; that is where the first real errors will show up.

**Nothing retrains itself.** Rejections are stored as labels and come out of
`/api/labels` in the right shape for a training run, and then a person has to
run it. A model that retrains overnight on its own review queue, unattended, is
a way to drift somewhere strange without anyone noticing.

**The queue has no age escalation.** A ₦900 payment that has been waiting three
weeks stays at the bottom forever, because the ordering is purely by money. That
is right for the books and wrong for the customer waiting on it.

**Settlement batches are read from the corpus, not from Paystack.** Against a
live account you would pull them from the settlement endpoints on a schedule.
That is not built.

**The generative extraction layer is wired up but not switched on.** The
interface, the schema constraint and the validation are all there and tested
with a stub. No model provider is configured, because a system that silently
starts calling an API is a system with a surprise invoice in it. The one report
in the labelled set that the rules cannot read — a comma-separated list with no
verb in it — is left failing rather than special-cased.

**It is one process holding everything in memory.** Fine for one shop and one
bookkeeper. Point `RECON_DATABASE_URL` at Postgres for anything larger.

**The container image has never been built.** The Dockerfile and the Cloud Run
steps are written and the packaging is tested, but there was no Docker daemon
available where this was developed, so the image itself is unverified.

---

## What is in here

| | |
| --- | --- |
| `recon.money` | Every amount, as a whole number of kobo. There is no float in the money path. |
| `recon.fees` | Paystack's Nigerian rates and the T+1 settlement gap. |
| `recon.paystack` | Webhook signature check, the six events we handle, and the verify call we actually believe. |
| `recon.ingest` / `recon.app` | The endpoint: signature, idempotency, 200 straight away, work in the background. |
| `recon.corpus` | Generates the month of payments *and* the answer key that says what each one really was. |
| `recon.match.deterministic` | The four certain rules, and the duplicate guard. |
| `recon.match.similarity` | Name matching that survives reordering and spelling variants without merging two customers. |
| `recon.match.model` / `.calibration` / `.threshold` | Eighteen named features, a logistic fit written out longhand, and a line drawn from a cost matrix. |
| `recon.intake` | Reads payment reports written by people, or refuses. |
| `recon.review` | The queue, the decisions, and the labels they produce. |
| `recon.report.settlement` | The daily report: gross, fees and timing shown separately rather than netted into one unexplained difference. |
| `recon.evaluation.harness` | `make eval`. Regenerates every number above. |

---

## Running it

```bash
make install
cp .env.example .env    # put your sk_test_ key in it
make corpus             # build the month of payments
make train              # fit the matcher
make eval               # every number above
make serve              # http://localhost:8000/review
```

Full instructions, including Cloud Run, in [`docs/DEPLOY.md`](docs/DEPLOY.md).
The design decisions, each with what was rejected and what it costs, are in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

### A few things about it on purpose

- **Test mode only.** The service refuses to start with an `sk_live_` key. It
  reads, matches and recommends, and it never moves money.
- **Every resolution is written to an append-only audit log** — what went in,
  which layer decided, what score, what evidence, when, and for a human
  decision, who.
- **Amounts are integers in kobo everywhere.** One float in the money path is a
  bug, and there is a test suite whose whole job is making sure there isn't one.
