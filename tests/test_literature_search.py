"""Search over the corpus. Mirrors the personal store's two-path design."""
from __future__ import annotations

import pytest

from health_agent.embeddings import HashingEmbedder
from health_agent.literature import corpus, embed, medline, schema
from health_agent.literature import store as lit_store
from health_agent.store import vector_store


@pytest.fixture
def built(tmp_path, literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, medline.parse_articles(literature_fixture.read_bytes()),
                 slug="test", version="1", license="CC-BY")
    store = vector_store.VectorStore(tmp_path / "vectors",
                                     table_name=embed.TABLE_NAME)
    embedder = HashingEmbedder()
    embed.embed_corpus(conn, store, embedder)
    yield conn, store, embedder
    conn.close()


def test_keyword_search_finds_a_known_article(built):
    conn, _, _ = built
    hits = lit_store.keyword_search(conn, "blood pressure exercise")
    assert hits
    assert any("40000001" == h.pmid for h in hits)
    assert all(h.method == "keyword" for h in hits)


def test_semantic_search_returns_findings(built):
    conn, store, embedder = built
    hits = lit_store.search(conn, store, embedder, "lowering blood pressure")
    assert hits
    assert all(h.method == "semantic" for h in hits)


def test_search_falls_back_to_keyword_when_the_vector_table_is_missing(built):
    """A missing or emptied vector table (e.g. `reset` reaching it, or a
    fresh checkout with no LanceDB directory yet) must degrade to working
    keyword search rather than silently returning nothing — matching the
    not-yet-embedded case just above, and `_search_records`' handling of the
    same situation for the personal store."""
    conn, store, embedder = built
    store.drop()  # simulate the vector table being gone
    hits = lit_store.search(conn, store, embedder, "blood pressure exercise")
    assert hits
    assert all(h.method == "keyword" for h in hits)


def test_every_finding_carries_its_tier_and_provenance(built):
    conn, _, _ = built
    for hit in lit_store.keyword_search(conn, "blood pressure"):
        assert hit.evidence_tier
        assert hit.tier_source in ("publication_type", "unmapped")
        assert hit.license


def test_min_tier_excludes_weaker_evidence(built):
    conn, _, _ = built
    # Unfiltered, "heart" matches two fixture articles weaker than rct
    # (narrative_review, observational). `min_tier="rct"` must exclude both.
    unfiltered = lit_store.keyword_search(conn, "heart")
    assert len(unfiltered) == 2
    hits = lit_store.keyword_search(conn, "heart", min_tier="rct")
    assert hits == []
    assert len(hits) < len(unfiltered)
    assert all(h.evidence_rank is not None and h.evidence_rank <= 3
               for h in hits)


def test_min_tier_excludes_unknown_rather_than_ranking_it_last(built):
    conn, _, _ = built
    hits = lit_store.keyword_search(conn, "case", min_tier="case_report")
    assert all(h.evidence_tier != "unknown" for h in hits)


def test_since_year_filters_by_publication_year(built):
    conn, _, _ = built
    hits = lit_store.keyword_search(conn, "blood", since_year=2020)
    assert all(h.year is None or h.year >= 2020 for h in hits)


def test_citation_is_dated_and_names_the_journal(built):
    conn, _, _ = built
    (hit, *_) = lit_store.keyword_search(conn, "blood pressure exercise")
    assert str(hit.year) in hit.citation
    assert "PMID" in hit.citation


def test_retracted_findings_are_returned_but_flagged(built):
    conn, _, _ = built
    hits = lit_store.keyword_search(conn, "withdrawn OR retracted OR study",
                                    limit=20)
    retracted = [h for h in hits if h.retracted]
    assert retracted, "the fixture's retracted article must still be findable"
    assert retracted[0].retraction_note


def test_results_are_ordered_by_evidence_strength_before_relevance(built):
    conn, _, _ = built
    hits = lit_store.keyword_search(conn, "blood pressure cholesterol sleep",
                                    limit=20)
    ranks = [h.evidence_rank for h in hits if h.evidence_rank is not None]
    assert ranks == sorted(ranks)
