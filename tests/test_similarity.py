"""Name similarity has one job that matters: tell two customers apart."""

from __future__ import annotations

import pytest

from recon.match.similarity import (
    folded_overlap,
    meaningful_tokens,
    name_similarity,
    surname_matches,
    token_overlap,
)


class TestItSurvivesTheUsualMess:
    @pytest.mark.parametrize(
        ("observed", "customer"),
        [
            ("OKONKWO ADA", "Ada Okonkwo"),  # the bank prints surname first
            ("ada okonkwo", "Ada Okonkwo"),  # case
            ("NIP/GTB/ADA OKONKWO/PAYMENT FOR GOODS", "Ada Okonkwo"),  # buried in a narration
            ("TRF FROM ADA OKONKWO", "Ada Okonkwo"),
            ("000013240517123456/ADA OKONKWO/TRANSFER", "Ada Okonkwo"),  # session id in front
            ("ADA  OKONKWO ", "Ada Okonkwo"),  # whitespace
        ],
    )
    def test_the_same_person_scores_top_marks(self, observed: str, customer: str) -> None:
        assert name_similarity(observed, customer) >= 0.95

    @pytest.mark.parametrize(
        ("observed", "customer"),
        [
            ("ADA OKONKO", "Ada Okonkwo"),  # dropped letter
            ("SEUN ADEYEMI", "Oluwaseun Adeyemi"),  # short form
            ("MUHAMMAD SANUSI", "Mohammed Sanusi"),  # accepted variant
            ("A. OKONKWO", "Ada Okonkwo"),  # initial
            ("AISHAT BELLO", "Aisha Bello"),
        ],
    )
    def test_a_recognisable_variant_still_scores_well(self, observed: str, customer: str) -> None:
        assert 0.6 <= name_similarity(observed, customer) < 1.0


class TestItTellsPeopleApart:
    """The whole point. A function that cannot do this is worse than none."""

    @pytest.mark.parametrize(
        ("observed", "right", "wrong"),
        [
            ("CHINEDUM OKAFOR", "Chinedum Okafor", "Chinedu Okafor"),
            ("CHINEDU OKAFOR", "Chinedu Okafor", "Chinedum Okafor"),
            ("AISHAT BELLO", "Aishat Bello", "Aisha Bello"),
            ("MOHAMMED SANUSI", "Mohammed Sanusi", "Muhammad Sanusi"),
            ("NGOZI EZEH", "Ngozi Ezeh", "Ngozi Eze"),
            ("TUNDE BALOGON", "Tunde Balogon", "Tunde Balogun"),
        ],
    )
    def test_the_exact_spelling_always_wins(self, observed: str, right: str, wrong: str) -> None:
        assert name_similarity(observed, right) > name_similarity(observed, wrong)

    def test_two_strangers_score_near_zero(self) -> None:
        assert name_similarity("TRF FROM BISI ADEWALE", "Ada Okonkwo") < 0.3

    def test_sharing_only_a_surname_is_not_a_match(self) -> None:
        assert name_similarity("IBRAHIM BELLO", "Aisha Bello") < 0.75

    def test_an_empty_side_scores_zero_rather_than_matching_everything(self) -> None:
        assert name_similarity("", "Ada Okonkwo") == 0.0
        assert name_similarity("TRF FROM", "Ada Okonkwo") == 0.0


class TestTokenHandling:
    def test_bank_words_and_noise_are_thrown_away(self) -> None:
        assert meaningful_tokens("NIP/GTB/ADA OKONKWO/PAYMENT FOR GOODS") == {"ada", "okonkwo"}

    def test_digits_are_not_names(self) -> None:
        assert meaningful_tokens("000013240517123456/ADA OKONKWO") == {"ada", "okonkwo"}

    def test_folding_only_happens_in_the_folded_view(self) -> None:
        assert token_overlap("MUHAMMAD SANUSI", "Mohammed Sanusi") == 0.5
        assert folded_overlap("MUHAMMAD SANUSI", "Mohammed Sanusi") == 1.0

    def test_surname_match_is_exact(self) -> None:
        assert surname_matches("TRF FROM ADA OKONKWO", "Ada Okonkwo")
        assert not surname_matches("TRF FROM ADA OKONKO", "Ada Okonkwo")


class TestItIsWellBehaved:
    @pytest.mark.parametrize(
        ("a", "b"),
        [("Ada Okonkwo", "Ada Okonkwo"), ("OKONKWO ADA", "Chinedu Eze"), ("", "")],
    )
    def test_the_score_is_always_between_zero_and_one(self, a: str, b: str) -> None:
        assert 0.0 <= name_similarity(a, b) <= 1.0

    def test_identical_strings_score_exactly_one(self) -> None:
        assert name_similarity("Ada Okonkwo", "Ada Okonkwo") == 1.0
