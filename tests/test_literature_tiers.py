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


@pytest.mark.parametrize("pub_types,expected,rank", [
    (["Clinical Trial"], "clinical_trial", 4),
    (["Clinical Trial, Phase I"], "clinical_trial", 4),
    (["Clinical Trial, Phase II"], "clinical_trial", 4),
    (["Clinical Trial, Phase III"], "clinical_trial", 4),
    (["Clinical Trial, Phase IV"], "clinical_trial", 4),
    (["Controlled Clinical Trial"], "clinical_trial", 4),
    (["Scoping Review"], "scoping_review", 5),
])
def test_new_mappings_from_real_pubmed_data(pub_types, expected, rank):
    tier, got_rank, source = tiers.resolve(pub_types)
    assert tier == expected
    assert got_rank == rank
    assert source == "publication_type"


def test_ranks_after_renumbering():
    """Pinned as a table so a later edit cannot quietly reorder the ladder."""
    assert tiers.TIERS == {
        "meta_analysis": 1,
        "systematic_review": 1,
        "guideline": 2,
        "rct": 3,
        "clinical_trial": 4,
        "narrative_review": 5,
        "scoping_review": 5,
        "observational": 6,
        "case_report": 7,
    }


def test_controlled_clinical_trial_is_not_an_rct():
    """PubMed's hierarchy: Clinical Trial > Controlled Clinical Trial >
    Randomized Controlled Trial. Controlled does not mean randomized, and
    slice 1's mapping of CCT to `rct` was a small credential inflation."""
    cct, cct_rank, _ = tiers.resolve(["Controlled Clinical Trial"])
    rct, rct_rank, _ = tiers.resolve(["Randomized Controlled Trial"])
    assert cct != rct
    assert cct_rank > rct_rank


def test_scoping_review_is_neither_systematic_nor_narrative():
    scoping, scoping_rank, _ = tiers.resolve(["Scoping Review"])
    _, systematic_rank, _ = tiers.resolve(["Systematic Review"])
    narrative, narrative_rank, _ = tiers.resolve(["Review"])
    assert scoping_rank > systematic_rank
    assert scoping_rank == narrative_rank
    assert scoping != narrative


def test_protocol_overrides_every_other_type():
    """Protocols of RCTs carry both `Clinical Trial Protocol` and `Randomized
    Controlled Trial`. Strongest-wins would mint an rct out of a plan that
    reports no results."""
    tier, rank, source = tiers.resolve(
        ["Journal Article", "Randomized Controlled Trial",
         "Clinical Trial Protocol"])
    assert tier == tiers.PROTOCOL == "protocol"
    assert rank is None
    assert source == "publication_type"


def test_protocol_is_unranked_so_a_floor_excludes_it():
    assert tiers.rank_of(tiers.PROTOCOL) is None
    assert tiers.tier_at_least(tiers.PROTOCOL, "case_report") is False


def test_protocol_is_not_unknown():
    """Both are unranked, and they are different facts: one is known and is
    not evidence, the other is not known. tier_source keeps them apart."""
    protocol = tiers.resolve(["Clinical Trial Protocol"])
    unknown = tiers.resolve(["Journal Article"])
    assert protocol[0] != unknown[0]
    assert protocol[2] == "publication_type"
    assert unknown[2] == "unmapped"


def test_validation_study_is_deliberately_unmapped():
    """Names a purpose, not a design. Mapping it would be inference by
    another name, so the decision is recorded rather than left looking like
    an omission."""
    assert "validation study" in tiers.DELIBERATELY_UNMAPPED
    tier, rank, source = tiers.resolve(["Validation Study"])
    assert tier == tiers.UNKNOWN
    assert rank is None
    assert source == "unmapped"


def test_deliberately_unmapped_types_never_appear_in_the_mapping():
    for raw in tiers.DELIBERATELY_UNMAPPED:
        assert tiers.resolve([raw])[0] == tiers.UNKNOWN
