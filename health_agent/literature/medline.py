"""MEDLINE XML -> article records. Bytes in, dataclasses out, no I/O.

Keeping the parse separate from the fetch is what lets slice 1 exist at all:
this module is exercised in full against a local fixture, and slice 2's network
code becomes a thin shim that hands it bytes. The smaller that shim, the more
credible the claim that no health question reaches the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from . import tiers


class MedlineParseError(ValueError):
    """Raised when the XML is not parseable MEDLINE."""


@dataclass
class ParsedArticle:
    pmid: str
    title: str
    abstract: str = ""
    doi: str | None = None
    journal: str | None = None
    pub_year: int | None = None
    publication_types: list[str] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    # The subset of mesh_terms MEDLINE itself marks MajorTopicYN="Y". Kept
    # as a second list rather than a flag per term so every existing reader
    # of mesh_terms stays untouched. See _mesh() for why the flag matters.
    major_terms: list[str] = field(default_factory=list)
    evidence_tier: str = tiers.UNKNOWN
    evidence_rank: int | None = None
    tier_source: str = "unmapped"
    retracted: bool = False
    retraction_note: str | None = None


def _text(node, path: str) -> str | None:
    found = node.find(path)
    if found is None:
        return None
    text = "".join(found.itertext()).strip()
    return text or None


def _abstract(article) -> str:
    """Join a structured abstract, keeping its section labels.

    MEDLINE abstracts are often split into labelled sections. Dropping the
    labels loses the distinction between what a study set out to do and what it
    found, which is exactly the distinction a reader of a cited finding needs.
    """
    parts: list[str] = []
    for node in article.findall("./Abstract/AbstractText"):
        body = "".join(node.itertext()).strip()
        if not body:
            continue
        label = (node.get("Label") or "").strip()
        parts.append(f"{label}: {body}" if label else body)
    return "\n\n".join(parts)


def _mesh(citation) -> tuple[list[str], list[str]]:
    """All MeSH descriptors, and the ones MEDLINE marks as major topics.

    Most headings on a real record are not about the subject at all: on
    2,000 abstracts the six most common were Humans, Male, Female, Middle
    Aged, Adult and Aged. MEDLINE distinguishes the subject headings itself
    with `MajorTopicYN="Y"` on the descriptor, so that distinction is read
    off the record and never guessed from a title. An absent attribute
    counts as "N": treating absence as major would put every check tag
    straight back into the topic list.
    """
    terms: list[str] = []
    major: list[str] = []
    for node in citation.findall("./MeshHeadingList/MeshHeading/DescriptorName"):
        term = "".join(node.itertext()).strip()
        if not term:
            continue
        terms.append(term)
        if (node.get("MajorTopicYN") or "").strip().upper() == "Y":
            major.append(term)
    return terms, major


def _year(article) -> int | None:
    """The year as the source states it, future years included.

    Ahead-of-print records carry the year of the issue they are scheduled
    for, so a fresh export can hold articles dated a year or two ahead (3 of
    2,000 in one build). That is what PubMed records, and a citation should
    match its source; clamping to today would make the stored year disagree
    with the record it claims to cite. coverage()'s year range therefore
    reports it as is.
    """
    raw = _text(article, "./Journal/JournalIssue/PubDate/Year")
    if raw and raw.isdigit():
        return int(raw)
    # PubDate sometimes carries only a MedlineDate such as "2019 Jan-Feb".
    medline_date = _text(article, "./Journal/JournalIssue/PubDate/MedlineDate")
    if medline_date:
        head = medline_date.strip()[:4]
        if head.isdigit():
            return int(head)
    return None


def _retraction(citation) -> tuple[bool, str | None]:
    for node in citation.findall("./CommentsCorrectionsList/CommentsCorrections"):
        if (node.get("RefType") or "") == "RetractionIn":
            source = _text(node, "./RefSource")
            return True, source or "retraction recorded, source not stated"
    return False, None


def parse_articles(xml_bytes: bytes) -> list[ParsedArticle]:
    """Parse a PubmedArticleSet. Articles without a PMID are skipped."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise MedlineParseError(f"could not parse MEDLINE XML: {exc}") from exc

    parsed: list[ParsedArticle] = []
    for entry in root.iter("PubmedArticle"):
        citation = entry.find("./MedlineCitation")
        if citation is None:
            continue
        pmid = _text(citation, "./PMID")
        article = citation.find("./Article")
        if not pmid or article is None:
            # No stable identifier means no citable record. Skipping is right;
            # a synthesized id would make an uncitable claim look citable.
            continue

        pub_types = [
            "".join(n.itertext()).strip()
            for n in article.findall("./PublicationTypeList/PublicationType")
            if "".join(n.itertext()).strip()
        ]
        tier, rank, source = tiers.resolve(pub_types)
        retracted, note = _retraction(citation)

        mesh_terms, major_terms = _mesh(citation)

        doi = None
        for node in entry.findall("./PubmedData/ArticleIdList/ArticleId"):
            if node.get("IdType") == "doi":
                doi = "".join(node.itertext()).strip() or None

        parsed.append(ParsedArticle(
            pmid=pmid,
            title=_text(article, "./ArticleTitle") or "(untitled)",
            abstract=_abstract(article),
            doi=doi,
            journal=_text(article, "./Journal/Title"),
            pub_year=_year(article),
            publication_types=pub_types,
            mesh_terms=mesh_terms,
            major_terms=major_terms,
            evidence_tier=tier,
            evidence_rank=rank,
            tier_source=source,
            retracted=retracted,
            retraction_note=note,
        ))
    return parsed
