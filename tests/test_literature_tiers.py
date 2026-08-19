"""Tiering is a lookup, not a judgement. These tests pin that down."""
from __future__ import annotations

import pytest

from health_agent.literature import tiers


@pytest.mark.parametrize("pub_types,expected", [
    (["Meta-Analysis"], "meta_analysis"),
    (["Systematic Review"], "systematic_review"),
    (["Practice Guideline"], "guideline"),
    (["Randomized Controlled Trial"], "rct"),
    (["Review"], "narrative_review"),
    (["Observational Study"], "observational"),
    (["Case Reports"], "case_report"),
])
def test_maps_publication_type_to_tier(pub_types, expected):
    tier, rank, source = tiers.resolve(pub_types)
    assert tier == expected
    assert source == "publication_type"
    assert rank is not None


def test_review_is_not_systematic_review():
    """The trap this table exists to avoid: PubMed tags a very large number of
    narrative reviews `Review`, and promoting them to the top tier would be
    exactly the credential inflation tiering is meant to prevent."""
    narrative, narrative_rank, _ = tiers.resolve(["Review"])
    systematic, systematic_rank, _ = tiers.resolve(["Systematic Review"])
    assert narrative != systematic
    assert narrative_rank > systematic_rank


def test_strongest_type_wins_when_several_are_present():
    tier, _, _ = tiers.resolve(["Journal Article", "Review", "Meta-Analysis"])
    assert tier == "meta_analysis"


def test_unmapped_types_are_unknown_never_a_guess():
    tier, rank, source = tiers.resolve(["Letter", "Published Erratum"])
    assert tier == tiers.UNKNOWN
    assert rank is None
    assert source == "unmapped"


def test_empty_publication_types_is_unknown():
    assert tiers.resolve([])[0] == tiers.UNKNOWN


def test_matching_ignores_case_and_surrounding_space():
    assert tiers.resolve(["  meta-analysis "])[0] == "meta_analysis"


def test_unknown_is_excluded_by_a_min_tier_filter_not_ranked_last():
    """Unranked, so a min_tier filter drops it rather than quietly admitting
    it at the bottom."""
    assert tiers.tier_at_least("meta_analysis", "rct") is True
    assert tiers.tier_at_least("case_report", "rct") is False
    assert tiers.tier_at_least(tiers.UNKNOWN, "case_report") is False
