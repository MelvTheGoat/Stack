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
