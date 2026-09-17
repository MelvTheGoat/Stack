# Reckon, explained without the jargon

## The problem

A small shop takes money four different ways — card, bank transfer, a bank
account number set aside for one specific customer, and cash over the counter.
Separately, it keeps a list of who owes what.

Every evening someone has to work out which payment paid which bill. That sounds
like it should take ten minutes. It doesn't, because most payments don't say
what they're for. A bank transfer arrives with one line of text — something like
`NIP/GTB/OKONKWO ADA/PAYMENT` — and that's everything you get.

**Why this matters:** when the matching goes wrong, a customer who already paid
gets chased for the money again. That's the bad outcome. Not a spreadsheet being
untidy — a real person being accused of not paying a bill they paid last
Tuesday.

## What it does

Think of it as sorting the evening's post.

Some envelopes have the invoice number written on the front. Easy — open, file,
done.

Some have no invoice number but arrived in a pigeonhole belonging to one specific
customer, who has exactly one unpaid bill of exactly that size. Also easy, for a
different reason.

The rest are the awkward ones. A name spelled slightly wrong. Someone who paid
about half of what they owe. One payment that covers three separate bills. The
same payment entered twice by mistake. Money from someone who doesn't owe
anything at all.

Reckon sorts the easy ones itself and **refuses to guess on the hard ones**. It
puts those in a pile for a person, biggest amounts first, with a note saying what
it thinks and why it isn't sure. The design rule is simple: only act alone when
you can prove you're right; otherwise hand it over.

It's deliberately arranged so the cheap, certain checks run first, and the
cleverer guessing only gets a look at what's left over.

## The result worth knowing

On a test month of **439 payments**, Reckon settled **76%** of them on its own
and got **none of them wrong**.

What that means for the person doing the job: checking all 439 by hand is about
**22 hours** of work. With this running, they check 105 and that's about **5
hours**. Roughly seventeen hours of someone's month handed back — and the 105
they do look at are sorted so the first evening's work covers 91% of the money
at stake.

The "none of them wrong" part matters more than the 76%. A system that
confidently files a payment under the wrong customer is worse than no system,
because nobody goes looking for a mistake they don't know about.

## The honest catch

**Those numbers come from invented data, not real customers.**

There was no real shop's payment history to test against — that's someone's
private financial records and not available to publish. So the test month is
generated: several hundred fake payments, built to fail in the ways real ones do
(misspelled names, part payments, duplicates), with a hidden answer key so the
results can be checked.

That makes the numbers *verifiable* — anyone can re-run them and get the same
result — but not *proven against reality*. How often real people type their
invoice number in, how often a real bank mangles a name: those are educated
guesses written into the test data. If the guesses are wrong, the percentages
move. The 76% is honest about what it measures. It just isn't a promise about
next month in a real shop.

One smaller catch: cash can't be verified by anything. If someone takes ₦20,000
and writes down ₦15,000, Reckon balances the books perfectly and is wrong — a
limit of the problem, not of the program.
