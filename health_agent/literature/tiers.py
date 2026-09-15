"""Evidence tier from the source's own metadata, and from nothing else.

A tier is read off MEDLINE's structured `PublicationType` field or it is
`unknown`. Nothing here inspects a title or an abstract, and nothing asks a
model. An inferred tier is a fabricated credential, and the feature's whole
value is that its citations can be trusted.

**`Review` is not `Systematic Review`.** PubMed applies `Review` to an enormous
number of narrative articles; mapping it to the top tier would inflate exactly
the credential this module exists to report honestly. Measured against 2,000
real abstracts, narrative reviews (12.4%) outnumbered systematic reviews and
meta-analyses combined (7.2%), so the split is worth roughly a tripling of the
apparent top tier. It gets rank 5 — below trials, above observational —
because an expert review is weak *evidence* but usually a reliable *summary*.

**Most of PubMed has no tier at all.** In the same sample 65.5% of articles
resolved to `unknown`, and five in six of those carried `Journal Article` and
nothing else. That is not a mapping gap to close: PubMed does not record a
study design for most primary research, and any tier assigned there would be
invented. It does mean a `min_tier` floor excludes most of a real corpus,
which the search tool now says out loud rather than leaving implicit.

**A protocol is not a result.** `Clinical Trial Protocol` overrides every
other type on the article, because protocols of RCTs are tagged with both and
strongest-wins would mint an `rct` out of a plan. It is unranked, like
`unknown`, but for the opposite reason: we know exactly what it is, and it is
not evidence.
"""

from __future__ import annotations

UNKNOWN = "unknown"
PROTOCOL = "protocol"

# tier key -> rank. Lower is stronger. Insertion order is strongest-first and
# is what the tool's `min_tier` enum is built from. `unknown` and `protocol`
# are deliberately absent: unranked rather than ranked last, so a min_tier
# filter excludes them instead of admitting them at the bottom.
TIERS: dict[str, int] = {
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

# MEDLINE PublicationType (lowercased) -> tier key.
#
# `Controlled Clinical Trial` sits under `clinical_trial`, not `rct`: in
# PubMed's own hierarchy it is the parent of `Randomized Controlled Trial`,
# and controlled does not mean randomized. The phase variants are PubMed's
# children of `Clinical Trial`; left unmapped, a Phase III trial would be
# `unknown`.
_BY_PUBLICATION_TYPE: dict[str, str] = {
    "meta-analysis": "meta_analysis",
    "systematic review": "systematic_review",
    "practice guideline": "guideline",
    "guideline": "guideline",
    "randomized controlled trial": "rct",
    "clinical trial": "clinical_trial",
    "clinical trial, phase i": "clinical_trial",
    "clinical trial, phase ii": "clinical_trial",
    "clinical trial, phase iii": "clinical_trial",
    "clinical trial, phase iv": "clinical_trial",
    "controlled clinical trial": "clinical_trial",
    "review": "narrative_review",
    "scoping review": "scoping_review",
    "observational study": "observational",
    "comparative study": "observational",
    "cohort studies": "observational",
    "case-control studies": "observational",
    "case reports": "case_report",
}

_PROTOCOL_TYPE = "clinical trial protocol"

# Types that appeared in real data and were considered and left unmapped,
# on purpose. `Validation Study` names what a study is for, not how it was
# designed — a validation study can be a cohort, a trial, or a case series —
# so any tier would be a guess. Listed so the decision reads as a decision.
DELIBERATELY_UNMAPPED: frozenset[str] = frozenset({
    "validation study",
})


def rank_of(tier: str) -> int | None:
    return TIERS.get(tier)


def _normalise(raw: object) -> str:
    return str(raw).strip().lower()


def resolve(publication_types: list[str]) -> tuple[str, int | None, str]:
    """Return `(tier, rank, tier_source)` for a MEDLINE publication type list.

    A protocol overrides everything else on the article. Otherwise, when
    several types map, the strongest wins — an article tagged both `Review`
    and `Meta-Analysis` is a meta-analysis.
    """
    normalised = [_normalise(raw) for raw in publication_types]
    if _PROTOCOL_TYPE in normalised:
        return PROTOCOL, None, "publication_type"

    best: str | None = None
    for raw in normalised:
        tier = _BY_PUBLICATION_TYPE.get(raw)
        if tier is None:
            continue
        if best is None or TIERS[tier] < TIERS[best]:
            best = tier
    if best is None:
        return UNKNOWN, None, "unmapped"
    return best, TIERS[best], "publication_type"


def tier_at_least(tier: str, min_tier: str) -> bool:
    """True when `tier` is at least as strong as `min_tier`.

    `unknown` and `protocol` are never at least anything: neither has a rank
    to compare.
    """
    rank, floor = rank_of(tier), rank_of(min_tier)
    if rank is None or floor is None:
        return False
    return rank <= floor
