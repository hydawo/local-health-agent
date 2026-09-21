"""Literature packs: what they are, and the file that carries one.

A pack is a single-topic slice of PubMed that a person installs as one
download. Two rules from the design are enforced by this module's shape:

**The catalog is code.** One `PackSpec` per pack drives both the
maintainer's build (the query) and the user's install (the asset name), so
the two can never describe different packs.

**Packs stay at the body-system level.** The download is itself a
disclosure: which pack a person fetches is visible to the host. `sleep` says
almost nothing about a person; a `type-2-diabetes` pack would say a lot.
Adding a pack below that line is a design change, not a catalog edit.

The file is gzipped JSON lines: a manifest, then one parsed article per
line. Parsed, not raw MEDLINE XML, because the parsed record is ~3 KB and
the XML ~25 KB, and nothing the tool uses is lost: `publication_types` stays
verbatim so the installer re-derives tiers itself rather than trusting the
pack. A parser fix is the maintainer's problem and means a new pack
version, which is the lineage unit the `pack` table was designed for.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .medline import ParsedArticle

FORMAT = 1

# The pack release version the installer defaults to. Bump this when a new
# pack release is cut; Task 4's installer reads it to pick what to fetch.
LATEST_VERSION = "2026.09"

# NLM's terms for MEDLINE citation records and abstracts. Not a Creative
# Commons licence: packs carry no full text. Written into article.license
# for every row of a pack, per LICENSES.md's rule that licenses are
# recorded per article, never asserted globally.
PACK_LICENSE = "MEDLINE/PubMed citation record and abstract; NLM terms of use"

# The evidence slice that keeps a topical pack shippable. `sample` uses no
# filter so the tiers show as they really are, `unknown` included.
STRONG_EVIDENCE: tuple[str, ...] = (
    "Meta-Analysis", "Systematic Review", "Practice Guideline", "Guideline",
    "Randomized Controlled Trial",
)


@dataclass(frozen=True)
class PackSpec:
    slug: str
    title: str
    description: str
    query: str                      # PubMed search term, MeSH based
    evidence_filter: tuple[str, ...] = ()
    since_year: int | None = 2015
    max_articles: int | None = None

    def search_term(self) -> str:
        """The full E-utilities term: topic, evidence slice, years, abstracts."""
        parts = [f"({self.query})"]
        if self.evidence_filter:
            types = " OR ".join(f'"{t}"[Publication Type]' for t in self.evidence_filter)
            parts.append(f"({types})")
        if self.since_year:
            parts.append(f"{self.since_year}:3000[dp]")
        parts.append("hasabstract[text]")
        return " AND ".join(parts)

    @property
    def sort(self) -> str | None:
        """The esearch sort order, or None for PubMed's default.

        Only a capped pack has one. An uncapped pack takes every match, so
        order is irrelevant; a capped one takes the first `max_articles`,
        and "most recent" is a description the reader can check, where
        "most relevant to a MeSH union" is not.
        """
        return "pub_date" if self.max_articles else None


_ALL_AREAS = (
    '"Cholesterol"[MeSH] OR "Hypertension"[MeSH] OR "Cardiovascular Diseases"[MeSH] '
    'OR "Diabetes Mellitus, Type 2"[MeSH] OR "Blood Glucose"[MeSH] OR "Obesity"[MeSH] '
    'OR "Sleep"[MeSH] OR "Exercise"[MeSH]'
)

CATALOG: dict[str, PackSpec] = {
    "sample": PackSpec(
        slug="sample", title="Sample",
        description="About 2,000 papers across all four areas, most recent by "
                    "publication date, for a first try.",
        query=_ALL_AREAS, evidence_filter=(), since_year=None, max_articles=2000),
    "cardiovascular": PackSpec(
        slug="cardiovascular", title="Cardiovascular",
        description="Blood pressure, cholesterol, HDL, LDL, cardiovascular disease.",
        query='"Hypertension"[MeSH] OR "Cholesterol"[MeSH] OR "Cholesterol, HDL"[MeSH] '
              'OR "Cholesterol, LDL"[MeSH] OR "Cardiovascular Diseases"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "metabolic": PackSpec(
        slug="metabolic", title="Metabolic",
        description="Blood glucose, HbA1c, type 2 diabetes, obesity, thyroid.",
        query='"Diabetes Mellitus, Type 2"[MeSH] OR "Blood Glucose"[MeSH] '
              'OR "Glycated Hemoglobin"[MeSH] OR "Obesity"[MeSH] OR "Thyroid Diseases"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "sleep": PackSpec(
        slug="sleep", title="Sleep",
        description="Sleep duration, sleep quality, sleep disorders.",
        query='"Sleep"[MeSH] OR "Sleep Wake Disorders"[MeSH] OR "Sleep Quality"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "exercise": PackSpec(
        slug="exercise", title="Exercise",
        description="Physical activity, fitness, training, recovery.",
        query='"Exercise"[MeSH] OR "Physical Fitness"[MeSH] OR "Exercise Therapy"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
}


@dataclass(frozen=True)
class Manifest:
    format: int
    slug: str
    version: str
    title: str
    description: str
    built_at: str
    article_count: int
    query: str
    evidence_filter: list[str]
    since_year: int | None
    source: str
    license: str
    sha256_of_articles: str


class PackError(RuntimeError):
    """A pack file that cannot be trusted: wrong format, or a digest mismatch."""


def asset_name(slug: str, version: str) -> str:
    return f"{slug}-{version}.jsonl.gz"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _article_line(article: ParsedArticle) -> str:
    return json.dumps(asdict(article), sort_keys=True, ensure_ascii=False)


def _article_from_raw(raw: dict) -> ParsedArticle:
    """Build ParsedArticle kwargs field by field, not `**raw`, so a pack
    written under an older `ParsedArticle` (missing a field added since)
    still loads: the missing field just takes its dataclass default."""
    kwargs = {}
    for f in dataclasses.fields(ParsedArticle):
        if f.name in raw:
            kwargs[f.name] = raw[f.name]
    return ParsedArticle(**kwargs)


def write_pack(path: Path, spec: PackSpec, version: str,
               articles: list[ParsedArticle], *, license: str) -> Manifest:
    """Write manifest + articles. The digest covers the article lines only,
    so the manifest can carry it."""
    body = "".join(_article_line(a) + "\n" for a in articles)
    manifest = Manifest(
        format=FORMAT, slug=spec.slug, version=version, title=spec.title,
        description=spec.description,
        built_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        article_count=len(articles), query=spec.search_term(),
        evidence_filter=list(spec.evidence_filter), since_year=spec.since_year,
        source="PubMed/MEDLINE via NCBI E-utilities", license=license,
        sha256_of_articles=sha256_text(body),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(manifest), sort_keys=True) + "\n")
        fh.write(body)
    return manifest


def read_pack(path: Path) -> tuple[Manifest, list[ParsedArticle]]:
    """Read and verify. A digest mismatch or unknown format raises before any
    article is returned, so an installer cannot half-trust a file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        first = fh.readline()
        body = fh.read()
    try:
        raw = json.loads(first)
    except json.JSONDecodeError as exc:
        raise PackError(f"{path.name}: first line is not a manifest") from exc
    if raw.get("format") != FORMAT:
        raise PackError(f"{path.name}: pack format {raw.get('format')!r}, "
                        f"this build reads format {FORMAT}")
    manifest = Manifest(**raw)
    if sha256_text(body) != manifest.sha256_of_articles:
        raise PackError(f"{path.name}: article digest does not match the manifest")
    # Split on "\n" only. `str.splitlines` also breaks on U+2028, U+0085 and
    # friends, and an abstract can contain them (the sleep pack's first
    # install died on one): the writer escapes nothing, so one JSON line
    # became two half-lines and a JSONDecodeError traceback.
    try:
        articles = [_article_from_raw(json.loads(line))
                    for line in body.split("\n") if line]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise PackError(f"{path.name}: an article line is not valid: {exc}") from exc
    if len(articles) != manifest.article_count:
        raise PackError(f"{path.name}: manifest says {manifest.article_count} "
                        f"articles, file holds {len(articles)}")
    return manifest, articles


__all__ = ["CATALOG", "FORMAT", "LATEST_VERSION", "Manifest", "PackError",
           "PackSpec", "PACK_LICENSE", "STRONG_EVIDENCE", "asset_name",
           "read_pack", "sha256_file", "sha256_text", "write_pack"]
