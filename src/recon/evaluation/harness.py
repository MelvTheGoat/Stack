"""`make eval` — every number in the README, regenerated from scratch.

The rule is that nothing goes in the README that this cannot reproduce. It
rebuilds the corpus, refits the model, runs both layers, scores the lot against
the answer key, and writes the numbers to `out/`. If a number in the README and
a number in `out/eval.json` disagree, the README is wrong.

Five things get measured, and the last two are the ones people skip:

1. **Coverage per layer** — what fraction each layer resolved.
2. **Precision and recall** — of what we closed, how much was right; and of what
   was findable, how much did we find.
3. **Calibration** — does a stated 80% mean 80%. Brier score, expected
   calibration error, and the reliability table.
4. **Extraction** — scored separately from matching, because they fail for
   different reasons.
5. **The adversarial slice** — duplicates, near-identical customer names, and
   payments that fit two invoices equally well. A system measured only on its
   average is measured on the easy cases.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recon.enums import DETERMINISTIC_LAYERS, Layer
from recon.evaluation import truth as truth_module
from recon.evaluation.extraction import compare as compare_extraction
from recon.evaluation.extraction import evaluate as evaluate_extraction
from recon.evaluation.extraction import load_labelled
from recon.evaluation.extraction import summarise as summarise_extraction
from recon.evaluation.truth import Truth
from recon.match.calibration import (
    brier_score,
    expected_calibration_error,
    reliability,
)
from recon.match.ledger import Ledger, TxnRow, transactions_from_fixtures
from recon.match.pipeline import Pipeline
from recon.match.result import Match
from recon.match.threshold import CostMatrix, precision_at_k, reviews_in_an_evening
from recon.match.training import (
    TrainedMatcher,
    residual_rows,
    split_by_time,
    train,
)
from recon.money import sum_money
from recon.review import queue as review_queue

#: The cases that exist to catch a matcher being confidently wrong.
ADVERSARIAL_CASES: frozenset[str] = frozenset(
    {"duplicate_submission", "ambiguous_twin_invoices", "no_matching_order"}
)


@dataclass
class Evaluation:
    numbers: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.numbers, indent=2, ensure_ascii=False) + "\n"


def run(fixtures: Path, *, with_model: bool = False) -> Evaluation:
    ledger = Ledger.from_fixtures(fixtures)
    transactions = transactions_from_fixtures(fixtures)
    answers = truth_module.load(fixtures / "ground_truth.json")

    trained = train(ledger, transactions, answers)
    matches = Pipeline.build(ledger, trained.build(ledger)).run(transactions)

    return Evaluation(
        {
            "corpus": _corpus(fixtures, transactions, answers),
            "layers": _layers(matches, answers),
            "matching": _matching(matches, answers),
            "calibration": _calibration(ledger, transactions, answers, trained),
            "threshold": _threshold(trained),
            "extraction": _extraction(fixtures, with_model=with_model),
            "adversarial": _adversarial(matches, answers),
            "review_queue": _queue(matches, transactions, answers),
            "against_doing_it_by_hand": _versus_manual(matches, answers, trained),
        }
    )


# ------------------------------------------------------------------ pieces


def _corpus(
    fixtures: Path, transactions: list[TxnRow], answers: dict[str, Truth]
) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((fixtures / "manifest.json").read_text())
    return {
        "seed": manifest.get("seed"),
        "period": manifest.get("generated_for_period"),
        "payments": len(transactions),
        "money": str(sum_money([txn.amount for txn in transactions])),
        "by_channel": manifest.get("transactions_by_channel"),
        "cases": dict(Counter(answer.case for answer in answers.values()).most_common()),
    }


def _layers(matches: list[Match], answers: dict[str, Truth]) -> dict[str, Any]:
    """What each layer resolved, and how right it was when it did.

    This is the table the architecture stands or falls on. If the probabilistic
    layer starts doing work the deterministic one could have done, it shows up
    here before it shows up anywhere else.
    """
    rows: dict[str, dict[str, Any]] = {}
    total = len(matches) or 1

    grouped: dict[str, list[Match]] = defaultdict(list)
    for match in matches:
        if match.needs_human or match.layer is Layer.UNRESOLVED:
            grouped["sent to a person"].append(match)
        else:
            grouped[str(match.layer)].append(match)

    order = [str(layer) for layer in Layer] + ["sent to a person"]
    for name in sorted(grouped, key=lambda n: order.index(n) if n in order else 99):
        group = grouped[name]
        correct = sum(
            1 for m in group if answers[m.transaction_reference].is_correct(m.order_references)
        )
        money = sum_money([m.money_at_risk for m in group])
        rows[name] = {
            "resolved": len(group),
            "share": round(len(group) / total, 4),
            "correct": correct if name != "sent to a person" else None,
            "precision": round(correct / len(group), 4) if name != "sent to a person" else None,
            "money": str(money),
        }

    deterministic = sum(
        row["resolved"]
        for name, row in rows.items()
        if name in {str(x) for x in DETERMINISTIC_LAYERS}
    )
    probabilistic = rows.get(str(Layer.PROBABILISTIC), {}).get("resolved", 0)
    human = rows.get("sent to a person", {}).get("resolved", 0)

    return {
        "per_rule": rows,
        "summary": {
            "deterministic": round(deterministic / total, 4),
            "probabilistic": round(probabilistic / total, 4),
            "a_person": round(human / total, 4),
        },
    }


def _matching(matches: list[Match], answers: dict[str, Truth]) -> dict[str, Any]:
    cleared = [m for m in matches if m.resolved]
    correct = [
        m for m in cleared if answers[m.transaction_reference].is_correct(m.order_references)
    ]

    findable = [m for m in matches if not answers[m.transaction_reference].pays_nothing]
    found = [
        m
        for m in findable
        if m.resolved and answers[m.transaction_reference].is_correct(m.order_references)
    ]

    by_case: dict[str, dict[str, Any]] = {}
    for case in sorted({answer.case for answer in answers.values()}):
        group = [m for m in matches if answers[m.transaction_reference].case == case]
        closed = [m for m in group if m.resolved]
        right = [
            m for m in closed if answers[m.transaction_reference].is_correct(m.order_references)
        ]
        by_case[case] = {
            "payments": len(group),
            "closed_unattended": len(closed),
            "of_those_correct": len(right),
            "went_to_a_person": len(group) - len(closed),
        }

    return {
        "auto_clear_rate": round(len(cleared) / len(matches), 4) if matches else 0.0,
        "precision": round(len(correct) / len(cleared), 4) if cleared else 0.0,
        "recall": round(len(found) / len(findable), 4) if findable else 0.0,
        "wrong_auto_clears": len(cleared) - len(correct),
        "money_closed": str(sum_money([m.money_at_risk for m in cleared])),
        "money_to_a_person": str(sum_money([m.money_at_risk for m in matches if not m.resolved])),
        "by_case": by_case,
    }


def _calibration(
    ledger: Ledger,
    transactions: list[TxnRow],
    answers: dict[str, Truth],
    trained: TrainedMatcher,
) -> dict[str, Any]:
    """Measured on payments the model never trained on."""
    rows = residual_rows(ledger, transactions, answers)
    _, held_out = split_by_time(rows)
    if not held_out:
        return {"note": "nothing held back"}

    probabilities = [
        trained.calibrator.calibrate(trained.model.predict(row.vector)) for row in held_out
    ]
    labels = [row.label for row in held_out]
    bins = reliability(probabilities, labels, bins=10)

    return {
        "method_chosen": str(trained.calibrator.to_dict()["kind"]),
        "brier_by_method_on_development_data": {
            name: round(value, 4) for name, value in trained.brier_by_method.items()
        },
        "held_out_rows": len(held_out),
        "held_out_brier": round(brier_score(probabilities, labels), 4),
        "expected_calibration_error": round(expected_calibration_error(bins), 4),
        "reliability": [
            {
                "band": f"{b.lower:.1f}-{b.upper:.1f}",
                "cases": b.count,
                "we_said": round(b.mean_predicted, 3),
                "it_actually_was": round(b.actual_rate, 3),
            }
            for b in bins
            if b.count
        ],
        "how_to_read_it": (
            "of the cases where we said about 80%, how many were actually right. "
            "If the two columns agree, the number means what it says."
        ),
    }


def _threshold(trained: TrainedMatcher) -> dict[str, Any]:
    return {
        "line": trained.threshold.threshold,
        "chosen_on_payments": trained.dev_payments,
        "how": (
            "four-fold cross-validation over the residual, so every payment is "
            "scored by a model that never saw it"
        ),
        "cost_matrix": CostMatrix().describe(),
        "at_that_line": trained.threshold.describe(),
    }


def _extraction(fixtures: Path, *, with_model: bool = False) -> dict[str, Any]:
    path = fixtures / "intake_labelled.json"
    if not path.exists():
        return {"note": "no labelled set"}

    rows = load_labelled(path)
    held_back = [row for row in rows if row.get("written_after")]
    out: dict[str, Any] = {
        "all": summarise_extraction(evaluate_extraction(rows)),
        "written_after_the_parser_was_finished": summarise_extraction(
            evaluate_extraction(held_back)
        ),
        "caveat": (
            f"{len(rows)} examples, written by the same person who wrote the parser. "
            f"{len(held_back)} of them were written afterwards and never used to tune it. "
            "The honest reading is 'it has not failed on these yet', not an accuracy rate."
        ),
    }
    if with_model:
        # Costs money, so it only runs when asked for: `make eval-llm`.
        out["with_the_model_layer"] = compare_extraction(rows)
    return out


def _adversarial(matches: list[Match], answers: dict[str, Truth]) -> dict[str, Any]:
    """The cases built to catch a matcher being confidently wrong.

    Scored separately because they are rare enough to vanish into an average,
    and they are exactly where a wrong auto-clear costs the most.
    """
    out: dict[str, Any] = {}
    for case in sorted(ADVERSARIAL_CASES):
        group = [m for m in matches if answers[m.transaction_reference].case == case]
        if not group:
            continue
        closed = [m for m in group if m.resolved]
        right = [
            m for m in closed if answers[m.transaction_reference].is_correct(m.order_references)
        ]
        out[case] = {
            "payments": len(group),
            "closed_unattended": len(closed),
            "of_those_correct": len(right),
            "wrongly_closed": len(closed) - len(right),
            "sent_to_a_person": len(group) - len(closed),
            "money": str(sum_money([m.money_at_risk for m in group])),
        }

    wrong = sum(row["wrongly_closed"] for row in out.values())
    out["verdict"] = (
        "no adversarial case was closed wrongly"
        if wrong == 0
        else f"{wrong} adversarial cases were closed wrongly"
    )
    return out


def _queue(
    matches: list[Match], transactions: list[TxnRow], answers: dict[str, Truth]
) -> dict[str, Any]:
    queue = review_queue.build(matches, transactions)
    budget = reviews_in_an_evening()

    # "Worth queueing" means the payment really was not settleable unattended:
    # either it pays nothing, or its answer was outside what a layer could
    # reach. A queued case that a layer could have closed is wasted time.
    ranked = [
        (
            item.money_at_risk,
            1
            if answers[item.transaction_reference].winnable_by in ("fuzzy", "human", "none")
            else 0,
        )
        for item in queue.items
    ]

    return {
        "waiting": len(queue.items),
        "money_at_risk": str(queue.money_at_risk),
        "an_evening_is": budget,
        "money_in_the_first_" + str(budget): str(queue.money_in_the_top(budget)),
        "share_of_the_money_in_that_evening": (
            round(queue.money_in_the_top(budget).kobo / queue.money_at_risk.kobo, 4)
            if queue.money_at_risk.kobo
            else 0.0
        ),
        f"precision_at_{budget}": round(precision_at_k(ranked, budget), 4),
        "suggestions_offered": sum(1 for item in queue.items if item.has_a_suggestion),
        "suspected_repeats": sum(1 for item in queue.items if item.is_suspected_duplicate),
    }


def _versus_manual(
    matches: list[Match], answers: dict[str, Truth], trained: TrainedMatcher
) -> dict[str, Any]:
    """What the month costs with this thing, against a person doing all of it.

    The manual baseline is not free and it is not perfect either. A person
    matching four hundred payments by hand at three minutes each is twenty
    hours, and the cases this system finds hard are the same ones they find
    hard. This comparison assumes the person makes no mistakes at all, which
    they would not, so the saving below is the pessimistic end.
    """
    costs = CostMatrix()
    queued = [m for m in matches if not m.resolved]
    cleared = [m for m in matches if m.resolved]
    wrong = [
        m for m in cleared if not answers[m.transaction_reference].is_correct(m.order_references)
    ]

    by_hand = costs.review * len(matches)
    reviewing = costs.review * len(queued)
    mistakes = costs.wrong_auto_clear * len(wrong)
    with_this = reviewing + mistakes

    return {
        "payments_in_the_month": len(matches),
        "if_a_person_checked_every_one": {
            "cost": str(by_hand),
            "hours": round(len(matches) * 3 / 60, 1),
        },
        "with_this_running": {
            "cost": str(with_this),
            "hours_of_review": round(len(queued) * 3 / 60, 1),
            "reviews": str(reviewing),
            "cleaning_up_wrong_clears": str(mistakes),
            "wrong_clears": len(wrong),
        },
        "saved": str(by_hand - with_this),
        "threshold_used": trained.threshold.threshold,
        "caveat": (
            "this assumes a person checking by hand never makes a mistake, "
            "which they would, so the saving is the pessimistic end"
        ),
    }


# --------------------------------------------------------------- printing


def as_markdown(numbers: dict[str, Any]) -> str:
    """The layered-resolution table, ready to paste into the README."""
    lines = ["| Layer | Resolved | Share | Right | Money |", "| --- | ---: | ---: | ---: | ---: |"]
    for name, row in numbers["layers"]["per_rule"].items():
        precision = "—" if row["precision"] is None else f"{row['precision']:.1%}"
        lines.append(
            f"| {name.replace('_', ' ')} | {row['resolved']} | "
            f"{row['share']:.1%} | {precision} | {row['money']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce every number in the README.")
    parser.add_argument("--fixtures", type=Path, default=Path("fixtures"))
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="also score the extraction layer with Claude switched on. Costs money.",
    )
    args = parser.parse_args(argv)

    evaluation = run(args.fixtures, with_model=args.with_model)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval.json").write_text(evaluation.to_json())
    (args.out / "layers.md").write_text(as_markdown(evaluation.numbers) + "\n")

    if not args.quiet:
        print(evaluation.to_json())
        print(as_markdown(evaluation.numbers))
    print(f"\nwritten to {args.out / 'eval.json'} and {args.out / 'layers.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
