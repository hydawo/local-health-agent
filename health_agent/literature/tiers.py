"""Evidence tier from the source's own metadata, and from nothing else.

A tier is read off MEDLINE's structured `PublicationType` field or it is
`unknown`. Nothing here inspects a title or an abstract, and nothing asks a
model. An inferred tier is a fabricated credential, and the feature's whole
value is that its citations can be trusted.

**`Review` is not `Systematic Review`.** PubMed applies `Review` to an enormous
number of narrative articles; mapping it to the top tier would inflate exactly
the credential this module exists to report honestly. It gets rank 4 — below
RCT, above observational — because an expert review is weak *evidence* but
usually a reliable *summary*. That is the least confident row in the table and
the one most likely to need revision against real data.
"""

from __future__ import annotations

UNKNOWN = "unknown"

# tier key -> rank. Lower is stronger. `unknown` is deliberately absent: it is
# unranked rather than ranked last, so a min_tier filter excludes it.
TIERS: dict[str, int] = {
    "meta_analysis": 1,
    "systematic_review": 1,
    "guideline": 2,
    "rct": 3,
    "narrative_review": 4,
    "observational": 5,
    "case_report": 6,
}

# MEDLINE PublicationType (lowercased) -> tier key.
_BY_PUBLICATION_TYPE: dict[str, str] = {
    "meta-analysis": "meta_analysis",
    "systematic review": "systematic_review",
    "practice guideline": "guideline",
    "guideline": "guideline",
    "randomized controlled trial": "rct",
    "controlled clinical trial": "rct",
    "review": "narrative_review",
    "observational study": "observational",
    "comparative study": "observational",
    "cohort studies": "observational",
    "case-control studies": "observational",
    "case reports": "case_report",
}


def rank_of(tier: str) -> int | None:
    return TIERS.get(tier)


def resolve(publication_types: list[str]) -> tuple[str, int | None, str]:
    """Return `(tier, rank, tier_source)` for a MEDLINE publication type list.

    When several types map, the strongest wins — an article tagged both
    `Review` and `Meta-Analysis` is a meta-analysis.
    """
    best: str | None = None
    for raw in publication_types:
        tier = _BY_PUBLICATION_TYPE.get(str(raw).strip().lower())
        if tier is None:
            continue
        if best is None or TIERS[tier] < TIERS[best]:
            best = tier
    if best is None:
        return UNKNOWN, None, "unmapped"
    return best, TIERS[best], "publication_type"


def tier_at_least(tier: str, min_tier: str) -> bool:
    """True when `tier` is at least as strong as `min_tier`.

    `unknown` is never at least anything: it has no rank to compare.
    """
    rank, floor = rank_of(tier), rank_of(min_tier)
    if rank is None or floor is None:
        return False
    return rank <= floor
