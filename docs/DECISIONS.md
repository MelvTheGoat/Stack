# Decisions

Each entry says what we picked, what we did not pick, and what it costs us.

---

## 1. Money is an integer number of kobo

**Picked:** every amount in the system is a whole number of kobo, wrapped in a
small `Money` class. ₦1,250.50 is stored as `125050`.

**Rejected:** Python `float`. Also rejected: `Decimal`.

**Why:** a float cannot hold `0.1` exactly. Add enough of them and your books
drift by a few kobo, and a few kobo across a few thousand rows is a real
argument with a real customer. `Decimal` is exact, but it lets you write
`Decimal("0.005")`, which is a third of a kobo, which is not a thing that
exists. Integers make the impossible amount unrepresentable.

**What it costs:** every boundary (Paystack JSON, CSV, the screen) needs an
explicit conversion. We pay that cost on purpose, in one file, with tests.

---

## 2. The verify call is the authority, not the webhook body

**Picked:** when a webhook arrives we check its signature, write the raw body
down, answer 200, and then go and ask Paystack `GET /transaction/verify/{ref}`
before the payment counts. Paystack's answer wins on amount, fees and status.

**Rejected:** trusting the signed webhook body on its own.

**Why:** the signature proves the body came from someone holding the secret key,
which is good but is a different question from "is this true now". A webhook is
a snapshot from the moment it was queued; it can be stale by the time it lands,
and it is the only part of the system an attacker gets to aim at directly. The
verify call costs one HTTP round trip that nobody is waiting on.

**What it costs:** a second call per event, and a new failure mode — Paystack
being unreachable. We tell those apart: "Paystack says no such reference" marks
the payment failed, "we could not ask" leaves it unverified and retryable. The
two must never collapse into one branch.

---

## 3. Receive and process are two steps with two sessions

**Picked:** the endpoint commits the raw event in its own session, answers 200,
and hands the event key to a background task that opens a fresh session.

**Rejected:** one request-scoped session shared by the handler and the worker.

**Why:** we measured it — Starlette runs background tasks *before* tearing down
a dependency that yields. So a request-scoped session has not committed yet
when the worker goes looking for the row, and the worker silently finds
nothing. It fails every time under the test client and would have failed
intermittently in production, which is worse.

**What it costs:** two connections per webhook instead of one, and the worker
has to re-read the row it was just handed. Worth it for not depending on the
teardown order of somebody else's framework.

---

## 4. A payment only ever moves forward

**Picked:** every transaction status has a rank, and an event is ignored unless
it moves the payment to a higher rank. pending → failed → success → reversed →
refunded.

**Rejected:** last-write-wins on `updated_at`.

**Why:** webhook order is not guaranteed, and `refund.processed` really does
arrive before the `charge.success` it refunds. Under last-write-wins the late
success un-refunds the payment and the books say we kept money we gave back.
Timestamps do not save you either, because the clock that stamped them is not
ours.

**What it costs:** a genuine reversal *after* a refund cannot be expressed, and
a payment can never be walked backwards to fix a mistake. Corrections have to be
new rows, which is the right answer for an audited ledger anyway.

---

## 5. The test corpus is generated, and the answer key ships with it

**Picked:** a seeded generator that produces a month of payments together with a
ground-truth file recording, for every transaction, which orders it really paid
and which kind of mess it is.

**Rejected:** a hand-written fixture file of a few dozen rows. Also rejected:
anonymised real data.

**Why:** you cannot report "the deterministic layer closes 58% of cases" unless
something knows what the right answer was for all of them. Hand-written
fixtures are too small and too tidy — they contain the cases you thought of,
which are the cases your matcher already handles. Real data would be better
still, but it is somebody's customer list and it is not ours to publish.

**What it costs:** the numbers describe a simulation, and the simulation's
weights (how often people type the invoice number in, how often names are
reversed) are a guess. Those weights are written out in one place in
`generate.py` rather than scattered through the code, so anyone can see what
was assumed and argue with it. If the guess is wrong, every coverage number
moves with it. That is a real limitation and the README says so.

---

## 6. The threshold is chosen by cross-validation, not on a held-out slice

**Picked:** four-fold cross-validation over the residual. Every payment gets a
score from a model that never saw it, and the threshold is chosen on all of
them.

**Rejected:** a straight train/calibrate/test split, which is what this started
as.

**Why:** the residual is about 120 payments. A 50/25/25 split left 29 payments
to choose a threshold on. At those 29, a line of 0.61 produced zero wrong
auto-clears — and on the full corpus that same line was wrong 15% of the time.
Twenty-nine samples with no observed mistakes says almost nothing when one
mistake costs eighty-three reviews. Cross-validation spends every payment on the
decision instead of three quarters of them on a model we then throw away.

**What it costs:** four model fits instead of one (about four seconds), and the
model that ships is not any of the four that were measured — it is refitted on
all the development data afterwards. That is standard, and it is still honest,
but it means the reported threshold was chosen for a slightly different model
than the one running. A bigger corpus would remove the need for the trick
entirely.

---

## 7. Calibration picks the smoother method on a near tie

**Picked:** identity, then Platt, then isotonic — and a method only displaces a
smoother one if it wins by more than 0.01 Brier.

**Rejected:** take whichever has the lowest Brier score.

**Why:** isotonic won by 0.001 and did it by collapsing every probability onto
six distinct values. Every threshold between 0.6 and 0.99 then cleared exactly
the same cases, so "where do we draw the line" stopped having an answer. A
scoring rule cannot see that, because a step function that is right on average
scores fine. Being a thousandth better at Brier is not worth losing the ability
to set a threshold at all.

**What it costs:** if the model's scores ever are badly shaped in a way only
isotonic can fix, we will take the worse Brier until the gap exceeds the
tolerance. The tolerance is a constant at the top of the file, and `make eval`
prints every method's score, so the trade is visible rather than buried.

---

## 8. The probabilistic layer only fires when it is nearly certain

**Picked:** the cost matrix puts a wrong auto-clear at 83 reviews, which puts
the threshold at 0.85 and means layer two closes only about 6% of the month on
its own.

**Rejected:** tuning the threshold to make the coverage number look better.

**Why:** at 83:1 you need to be wrong less than about 1.2% of the time for an
auto-clear to pay. The model is not that good on the hard residual, and pushing
the line down to clear more would trade five minutes of a bookkeeper's evening
for a two-hour cleanup and an awkward phone call. Six percent that is right is
worth more than twenty percent that is mostly right.

**What it costs:** the headline coverage is 76%, not 90%. The remaining quarter
is genuinely hard — part payments, split payments, duplicates and twin invoices
— and a person looks at it. That is the correct answer for this data, and if a
bigger corpus later supports a lower line, the cost matrix will say so on its
own.
