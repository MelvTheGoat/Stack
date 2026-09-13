"""The corpus is the repo's evidence, so it gets checked like evidence.

These tests do not test the generator's code so much as the properties of what
it produces: the money is exact, the answer key lines up, and every failure mode
we claim to cover is actually in there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from recon.corpus.generate import DEFAULT_SEED, Corpus, build, write
from recon.enums import Channel
from recon.fees import fee_for
from recon.money import Money, sum_money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return build(DEFAULT_SEED)


class TestItIsReproducible:
    def test_the_same_seed_gives_the_same_corpus(self) -> None:
        first, second = build(99), build(99)
        assert first.transactions == second.transactions
        assert first.orders == second.orders
        assert [t.transaction_reference for t in first.truth] == [
            t.transaction_reference for t in second.truth
        ]

    def test_a_different_seed_gives_a_different_corpus(self) -> None:
        assert build(1).transactions != build(2).transactions

    def test_the_committed_fixtures_match_the_generator(self) -> None:
        """If this fails, someone edited fixtures by hand or changed the generator
        without running `make corpus`. Either way the README numbers are stale."""
        if not (FIXTURES / "transactions.json").exists():
            pytest.skip("fixtures not generated yet")
        on_disk = json.loads((FIXTURES / "transactions.json").read_text())
        assert on_disk == build(DEFAULT_SEED).transactions


class TestMoneyIsAlwaysWhole:
    def test_no_float_survives_anywhere_in_the_corpus(self, corpus: Corpus) -> None:
        def walk(node: Any, path: str) -> None:
            if isinstance(node, float):
                raise AssertionError(f"a float got into the corpus at {path}: {node}")
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(corpus.orders, "orders")
        walk(corpus.transactions, "transactions")
        walk(corpus.settlements, "settlements")

    def test_every_amount_is_positive_kobo(self, corpus: Corpus) -> None:
        for txn in corpus.transactions:
            assert isinstance(txn["amount_kobo"], int)
            assert txn["amount_kobo"] > 0

    def test_fees_match_the_published_rates(self, corpus: Corpus) -> None:
        for txn in corpus.transactions:
            expected = fee_for(Channel(txn["channel"]), Money(txn["amount_kobo"]))
            assert Money(txn["fees_kobo"]) == expected, txn["reference"]


class TestTheAnswerKeyLinesUp:
    def test_every_transaction_has_exactly_one_truth_entry(self, corpus: Corpus) -> None:
        references = [t["reference"] for t in corpus.transactions]
        truth_references = [t.transaction_reference for t in corpus.truth]
        assert sorted(references) == sorted(truth_references)

    def test_every_order_the_truth_names_really_exists(self, corpus: Corpus) -> None:
        known = {o["reference"] for o in corpus.orders}
        for entry in corpus.truth:
            for reference in entry.order_references:
                assert reference in known, f"{entry.transaction_reference} points at a ghost"

    def test_transaction_references_are_unique(self, corpus: Corpus) -> None:
        references = [t["reference"] for t in corpus.transactions]
        assert len(references) == len(set(references))

    def test_no_customer_id_leaks_into_a_transaction(self, corpus: Corpus) -> None:
        """The matcher has to earn the attribution. If the answer were sitting in
        the transaction row, every coverage number would be meaningless."""
        for txn in corpus.transactions:
            assert "customer_id" not in txn


class TestEveryFailureModeIsPresent:
    REQUIRED = (
        "card_with_reference",
        "transfer_with_reference",
        "dva_exact",
        "dva_partial",
        "transfer_name_exact",
        "transfer_name_partial",
        "transfer_name_over",
        "split_three_invoices",
        "cash",
        "duplicate_submission",
        "ambiguous_twin_invoices",
        "no_matching_order",
    )

    @pytest.mark.parametrize("case", REQUIRED)
    def test_the_case_is_in_the_corpus(self, corpus: Corpus, case: str) -> None:
        count = sum(1 for t in corpus.truth if t.case == case)
        assert count > 0, f"the corpus claims to cover {case} but contains none"

    def test_there_is_a_refund_and_a_reversal(self, corpus: Corpus) -> None:
        statuses = [t["status"] for t in corpus.transactions]
        assert statuses.count("refunded") > 0
        assert statuses.count("reversed") > 0

    def test_all_four_channels_are_used(self, corpus: Corpus) -> None:
        channels = {t["channel"] for t in corpus.transactions}
        assert channels == {"card", "dva", "bank_transfer", "cash"}

    def test_the_corpus_is_several_hundred_transactions(self, corpus: Corpus) -> None:
        assert len(corpus.transactions) >= 300

    def test_names_arrive_mangled_in_several_different_ways(self, corpus: Corpus) -> None:
        manglings = {t.name_mangling for t in corpus.truth if t.name_mangling}
        assert manglings >= {"reorder", "alternative_spelling", "typo", "short_form"}


class TestTheHardCasesAreReallyHard:
    def test_a_split_payment_totals_its_invoices_exactly(self, corpus: Corpus) -> None:
        orders = {o["reference"]: o for o in corpus.orders}
        transactions = {t["reference"]: t for t in corpus.transactions}
        splits = [t for t in corpus.truth if t.case == "split_three_invoices"]
        assert splits
        for entry in splits:
            assert len(entry.order_references) == 3
            total = sum_money([Money(orders[r]["amount_kobo"]) for r in entry.order_references])
            assert Money(transactions[entry.transaction_reference]["amount_kobo"]) == total

    def test_a_duplicate_has_a_new_reference_but_the_same_money(self, corpus: Corpus) -> None:
        """A webhook replay is caught by idempotency. This is the other kind:
        the same money submitted again under a fresh reference."""
        transactions = {t["reference"]: t for t in corpus.transactions}
        duplicates = [t for t in corpus.truth if t.case == "duplicate_submission"]
        assert duplicates
        for entry in duplicates:
            copy = transactions[entry.transaction_reference]
            assert entry.duplicate_of is not None
            original = transactions[entry.duplicate_of]
            assert copy["reference"] != original["reference"]
            assert copy["amount_kobo"] == original["amount_kobo"]
            assert copy["narration"] == original["narration"]
            assert entry.order_references == [], "a duplicate pays no new invoice"

    def test_twin_invoices_are_genuinely_indistinguishable(self, corpus: Corpus) -> None:
        orders = {o["reference"]: o for o in corpus.orders}
        twins = [t for t in corpus.truth if t.case == "ambiguous_twin_invoices"]
        assert twins
        for entry in twins:
            first, second = (orders[r] for r in entry.order_references)
            assert first["amount_kobo"] == second["amount_kobo"]
            assert first["customer_id"] == second["customer_id"]
            assert entry.winnable_by == "human"

    def test_payments_that_match_nothing_are_labelled_as_such(self, corpus: Corpus) -> None:
        strays = [t for t in corpus.truth if t.case == "no_matching_order"]
        assert strays
        assert all(t.order_references == [] for t in strays)
        assert all(t.winnable_by == "none" for t in strays)

    def test_there_are_customers_whose_names_are_one_letter_apart(self, corpus: Corpus) -> None:
        confusable = [c for c in corpus.customers if "confusable_with" in c]
        assert len(confusable) >= 8
        names = {c["name"] for c in corpus.customers}
        for customer in confusable:
            assert customer["confusable_with"] in names

    def test_a_partial_payment_is_less_than_its_invoice(self, corpus: Corpus) -> None:
        orders = {o["reference"]: o for o in corpus.orders}
        transactions = {t["reference"]: t for t in corpus.transactions}
        partials = [t for t in corpus.truth if t.case.endswith("_partial")]
        assert partials
        for entry in partials:
            paid = transactions[entry.transaction_reference]["amount_kobo"]
            owed = orders[entry.order_references[0]]["amount_kobo"]
            assert paid < owed


class TestSettlements:
    def test_net_is_gross_minus_fees_minus_refunds(self, corpus: Corpus) -> None:
        by_batch: dict[str, list[dict[str, Any]]] = {}
        for txn in corpus.transactions:
            if txn["settlement_id"]:
                by_batch.setdefault(txn["settlement_id"], []).append(txn)

        for batch in corpus.settlements:
            members = by_batch[batch["id"]]
            gross = sum_money([Money(t["amount_kobo"]) for t in members])
            fees = sum_money([Money(t["fees_kobo"]) for t in members])
            refunds = sum_money([Money(t["refunded_kobo"]) for t in members])
            assert Money(batch["gross_kobo"]) == gross
            assert Money(batch["fees_kobo"]) == fees
            assert Money(batch["net_kobo"]) == gross - fees - refunds

    def test_only_card_and_dva_money_goes_through_paystack(self, corpus: Corpus) -> None:
        for txn in corpus.transactions:
            if txn["channel"] in ("cash", "bank_transfer"):
                assert txn["settlement_id"] is None, "that money never touched Paystack"

    def test_settlement_lands_after_the_payment(self, corpus: Corpus) -> None:
        from datetime import datetime

        batches = {b["id"]: b for b in corpus.settlements}
        for txn in corpus.transactions:
            if not txn["settlement_id"]:
                continue
            paid = datetime.fromisoformat(txn["paid_at"])
            settled = datetime.fromisoformat(batches[txn["settlement_id"]]["settled_at"])
            assert settled > paid, "money cannot arrive before it is taken"

    def test_settlement_never_lands_on_a_weekend(self, corpus: Corpus) -> None:
        from datetime import datetime

        for batch in corpus.settlements:
            assert datetime.fromisoformat(batch["settled_at"]).weekday() < 5


def test_write_produces_every_file(tmp_path: Path, corpus: Corpus) -> None:
    write(corpus, tmp_path)
    expected = {
        "customers.json",
        "dedicated_accounts.json",
        "orders.json",
        "transactions.json",
        "settlements.json",
        "ground_truth.json",
        "manifest.json",
    }
    assert {p.name for p in tmp_path.iterdir()} == expected
    for path in tmp_path.iterdir():
        json.loads(path.read_text())
