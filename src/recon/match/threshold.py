"""Where to draw the line between "close it" and "ask a person".

A threshold is not a statistical choice, it is a business one, and it only has
an answer once you say what each kind of mistake costs. So the costs are written
down here, in naira, with the reasoning next to them, and anyone who disagrees
can change two numbers and re-run `make eval`.

The two mistakes are not symmetrical:

* **A wrong auto-clear.** The books say invoice 214 is settled when it is not.
  Nobody notices until the customer is chased for money they already paid, or
  until month-end does not balance. Someone then has to find it, unpick it, and
  apologise. Call it two hours of work and an awkward phone call.

* **A wasted review.** A match that was actually fine gets put in front of a
  person, who looks at it for a couple of minutes and clicks approve. Annoying,
  not damaging.

At the numbers below that is a ratio of about 83 to 1, which is why the
threshold lands high. If reviews were free the threshold would be 1.0 and every
case would be looked at; if wrong clears were free it would be 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from recon.money import Money

#: What one person-hour of bookkeeping costs this business. A full-time
#: bookkeeper on about ₦190,000 a month, over roughly 160 working hours.
HOURLY_RATE = Money.from_naira("1200")

#: Three minutes to look at a queued match, read the evidence, and decide.
REVIEW_MINUTES = 3

#: Two hours to notice a wrong clear, trace it, reverse it, and call the
#: customer. Plus something for the customer being annoyed, which is real even
#: though it is not on any invoice.
UNPICKING_MINUTES = 120
GOODWILL_COST = Money.from_naira("2600")


def _cost_of(minutes: int) -> Money:
    return HOURLY_RATE.percent_bps(minutes * 10_000 // 60, rounding="half_up")


COST_OF_A_REVIEW = _cost_of(REVIEW_MINUTES)
COST_OF_A_WRONG_AUTO_CLEAR = _cost_of(UNPICKING_MINUTES) + GOODWILL_COST


@dataclass(frozen=True, slots=True)
class CostMatrix:
    """What each outcome costs. Change these, not the threshold."""

    review: Money = COST_OF_A_REVIEW
    wrong_auto_clear: Money = COST_OF_A_WRONG_AUTO_CLEAR

    @property
    def ratio(self) -> float:
        return self.wrong_auto_clear.kobo / self.review.kobo

    def describe(self) -> dict[str, str]:
        return {
            "a review costs": str(self.review),
            "a wrong auto-clear costs": str(self.wrong_auto_clear),
            "ratio": f"{self.ratio:.0f}:1",
            "assumption": (
                f"{REVIEW_MINUTES} minutes to check one match, {UNPICKING_MINUTES} minutes "
                f"to unpick a wrong one, at {HOURLY_RATE} an hour, plus {GOODWILL_COST} "
                "for the customer being annoyed"
            ),
        }


@dataclass(frozen=True, slots=True)
class ThresholdChoice:
    threshold: float
    expected_cost: Money
    auto_cleared: int
    sent_to_review: int
    wrong_auto_clears: int
    cost_of_reviewing_everything: Money
    cost_of_clearing_everything: Money

    @property
    def saving_against_manual(self) -> Money:
        """What this threshold saves against a person checking every single case."""
        return self.cost_of_reviewing_everything - self.expected_cost

    def describe(self) -> dict[str, str | float | int]:
        return {
            "threshold": round(self.threshold, 3),
            "auto_cleared": self.auto_cleared,
            "sent_to_review": self.sent_to_review,
            "wrong_auto_clears": self.wrong_auto_clears,
            "expected_cost_a_night": str(self.expected_cost),
            "if_a_person_checked_everything": str(self.cost_of_reviewing_everything),
            "if_we_cleared_everything_blind": str(self.cost_of_clearing_everything),
            "saving_against_checking_everything": str(self.saving_against_manual),
        }


def expected_cost(
    probabilities: list[float], labels: list[int], threshold: float, costs: CostMatrix
) -> Money:
    """What one night costs at this threshold, given how it actually went."""
    total = Money.zero()
    for probability, label in zip(probabilities, labels, strict=True):
        if probability >= threshold:
            if label == 0:
                total = total + costs.wrong_auto_clear
        else:
            total = total + costs.review
    return total


def choose_threshold(
    probabilities: list[float],
    labels: list[int],
    costs: CostMatrix | None = None,
    *,
    step: float = 0.01,
) -> ThresholdChoice:
    """Try every threshold and keep the cheapest.

    A sweep rather than a formula, because it is 100 evaluations on a few
    hundred rows and the result is easier to argue with than a derivation.
    """
    costs = costs or CostMatrix()
    if not probabilities:
        raise ValueError("cannot choose a threshold with nothing to choose it on")

    best: tuple[Money, float] | None = None
    candidates = [round(i * step, 4) for i in range(int(1 / step) + 1)]
    for threshold in candidates:
        cost = expected_cost(probabilities, labels, threshold, costs)
        # Ties go to the higher threshold: when two lines cost the same, the one
        # that shows a person more cases is the safer one to be wrong about.
        if best is None or cost < best[0] or (cost == best[0] and threshold > best[1]):
            best = (cost, threshold)

    assert best is not None
    cost, threshold = best
    cleared = [(p, y) for p, y in zip(probabilities, labels, strict=True) if p >= threshold]

    return ThresholdChoice(
        threshold=threshold,
        expected_cost=cost,
        auto_cleared=len(cleared),
        sent_to_review=len(probabilities) - len(cleared),
        wrong_auto_clears=sum(1 for _, y in cleared if y == 0),
        cost_of_reviewing_everything=costs.review * len(probabilities),
        cost_of_clearing_everything=expected_cost(probabilities, labels, 0.0, costs),
    )


def precision_at_k(ranked: list[tuple[Money, int]], k: int) -> float:
    """Of the top k cases in the queue, how many were worth a person's time.

    `ranked` is (money at risk, was this case genuinely worth queueing), already
    in the order the queue shows them. The question it answers is the one a
    bookkeeper asks at 7pm: if I only get through 40 of these tonight, am I
    working on the right 40?
    """
    if k <= 0 or not ranked:
        return 0.0
    top = ranked[:k]
    return sum(label for _, label in top) / len(top)


def reviews_in_an_evening(minutes: int = 120) -> int:
    """How many queued cases one person gets through after closing."""
    return minutes // REVIEW_MINUTES
