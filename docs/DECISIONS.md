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

---

## 9. A payment report that cannot be read goes to a person, once

**Picked:** rules first, an optional model second, and then a person. One
attempt per layer. A failure carries the original text, what was missing, and
what each layer said about it.

**Rejected:** retrying the model with a firmer prompt, or a "best effort" record
with the missing fields left blank.

**Why:** a second attempt at an ambiguous sentence does not produce more
information. It produces a more confident guess. "Someone paid 20k today" does
not contain a payer, and no amount of prompting will find one — but a model
asked twice will eventually supply a name, and that name goes into the books
looking exactly like a fact. A blank-field record is the same problem wearing a
different hat: "no date given" and "the date was 31/02/2024 and we dropped it"
are different things and the ledger cannot tell them apart afterwards.

**What it costs:** more items in the review queue. On the labelled set, one
readable report in eighteen gets refused when a person could have read it — the
comma-separated form with no verb in it, "invoice 42: forty-five thousand naira,
Ada Okonkwo, cash". That one is exactly what the generative layer is for, and it
is left failing in the repo rather than special-cased, because a rule written to
pass one test case is not a rule.

---

## 10. The extraction score is measured on a set we partly wrote afterwards

**Picked:** forty hand-labelled reports, ten of them written *after* the parser
was finished and never used to tune it. Those ten are marked
`written_after: true` in the fixture and reported separately.

**Rejected:** one labelled set, written alongside the parser.

**Why:** the first thirty examples scored 100%, and they scored 100% because the
rules were adjusted until they did. That is a smoke test wearing an evaluation's
clothes. The ten written afterwards immediately found four real bugs: hyphenated
number words, two invoice numbers in one sentence, an impossible date being
silently dropped, and "Ada Okonkwo's brother" being read as Ada Okonkwo.

**What it costs:** ten examples is still not many, and the same person wrote the
parser and the labels. The honest description of this number is "it has not
failed yet on forty examples", not "94% accurate". The README says that.

---

## 11. The review queue is ordered by money, not by confidence

**Picked:** biggest money at risk first, and the page says how much of tonight's
total the first forty cases cover.

**Rejected:** ordering by confidence (most-uncertain first), or by age.

**Why:** a bookkeeper has about two hours after closing, which is roughly forty
decisions. Ordering by confidence spends that evening on whatever the model
happens to find hardest, which is not the same as whatever matters. On this
corpus the first forty cases by money cover most of the money at risk; the same
forty by confidence would not. Age ordering has the same problem and also buries
a large payment behind a fortnight of small ones.

**What it costs:** a ₦900 payment that has been sitting there for three weeks
stays at the bottom forever. That is the right call for the money and the wrong
one for the customer waiting on it, so a real deployment would want an age
escalation. It does not have one yet, and the limitations section says so.

---

## 12. Rejections are stored as labels, approvals mostly are not

**Picked:** a rejection records the reason code, what the model had suggested,
and how confident it was. That is a labelled example of a specific mistake.

**Rejected:** treating approve and reject as symmetrical feedback.

**Why:** an approval usually confirms something the model was already fairly
sure about, and there are hundreds of those. A rejection is a case the model got
wrong, with a person's explanation of *how* it was wrong attached — wrong
customer, wrong amount, already paid. Those are rare and they are the only
examples that would actually move the model. Storing both equally would bury
them.

**What it costs:** the feedback loop is not closed yet. The labels come out of
`/api/labels` in the right shape for a training run, and nothing retrains on
them automatically. That is deliberate — a model that retrains itself on its own
review queue overnight, unattended, is a way to drift somewhere strange without
anyone noticing.
