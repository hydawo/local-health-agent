# Licenses

This covers the medical literature corpus (`health-agent literature build`),
not the codebase — see [LICENSE](LICENSE) for that.

## Abstracts and metadata vs. full text

Every article indexed carries its **abstract and MEDLINE metadata**: title,
journal, publication year, publication types (the source of the evidence
tier), DOI, MeSH headings. Those come from MEDLINE/PubMed's own citation
records, which NLM makes available for exactly this kind of reuse.

**Full text** is a different matter and is retained only when two things are
both true: the article is in the PMC Open Access subset, and that article's own
license permits it. Most of the corpus has no full text — an abstract and a
citation, and nothing more, which is what `search_medical_literature` returns
for those articles.

## Licenses are recorded per article, not asserted globally

`article.license` holds the license string for each row, and there is no
corpus-wide license claim anywhere in this codebase. That is deliberate, not an
oversight: the PMC Open Access subset itself mixes CC-BY, CC-BY-NC, and
CC-BY-NC-ND terms article by article, so a blanket statement like "the corpus
is CC-BY" would be false for whichever fraction of it isn't. Anything that
reads a license off the corpus — a citation, a coverage report, a future export
— reads it from that column, per article, never from a constant.

`article.full_text_available` records the separate decision of whether full
text was actually retained for that row. An article can have a known, permissive
license and still have no full text stored, if it was never fetched or if the
build chose abstract-only for that pack; the two columns are independent and
both are checked before anything beyond the abstract is surfaced.

## The fixture corpus is synthetic

`tests/fixtures/literature/corpus.xml` is **entirely invented** — written for
this repository to exercise the parser, the tiering logic, and retrieval. Its
PMIDs (`400000xx`), journal names ("Synthetic Reviews in Sleep Medicine"),
DOIs, and abstract text do not correspond to any real MEDLINE record. It
redistributes nothing: there is no real article behind any row in it, licensed
or otherwise. Tests and the demo run against it so that neither requires
network access or a real corpus build.

## ClinicalTrials.gov (slice 2)

Slice 1 does not fetch ClinicalTrials.gov records. When a later release adds
them (ROADMAP #1's acquisition half), they are marked accordingly: records
originating from ClinicalTrials.gov are **US Government works and carry no
copyright restriction** in the United States, distinct from the mixed
licensing that applies to PMC Open Access full text. That distinction is
recorded per record, the same way `article.license` already is, rather than
folded into the same field as a copyrighted work's license string.

## MeSH and the MEDLINE schema

MeSH (Medical Subject Headings) and the MEDLINE/PubMed citation schema this
corpus's metadata is drawn from are products of the U.S. National Library of
Medicine (NLM). Their use here does not imply NLM's endorsement of this tool,
and NLM has not reviewed or approved it.
