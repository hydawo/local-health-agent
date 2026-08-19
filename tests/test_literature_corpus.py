"""Building a corpus from parsed articles."""
from __future__ import annotations

from health_agent.literature import corpus, medline, schema


def _articles(fixture_path):
    return medline.parse_articles(fixture_path.read_bytes())


def test_build_writes_articles_chunks_and_a_pack(tmp_path, literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    stats = corpus.build(conn, _articles(literature_fixture),
                         slug="test", version="1", license="CC-BY")

    assert stats.articles > 0
    assert stats.chunks >= stats.articles
    pack = conn.execute("SELECT * FROM pack").fetchone()
    assert pack["slug"] == "test"
    assert pack["article_count"] == stats.articles
    conn.close()


def test_articles_without_an_abstract_are_skipped(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    empty = medline.ParsedArticle(pmid="1", title="No abstract", abstract="")
    stats = corpus.build(conn, [empty], slug="t", version="1", license="CC-BY")

    # Nothing to retrieve means nothing to cite. A title-only record would be
    # findable and then unusable.
    assert stats.articles == 0
    assert stats.skipped == 1
    conn.close()


def test_build_is_idempotent_for_a_repeated_pmid(tmp_path, literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    articles = _articles(literature_fixture)
    corpus.build(conn, articles, slug="t", version="1", license="CC-BY")
    corpus.build(conn, articles, slug="t", version="1", license="CC-BY")

    count = conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"]
    assert count == len(
        [a for a in articles if a.abstract.strip()])
    conn.close()


def test_rebuild_with_a_changed_pmid_refreshes_the_stored_row(tmp_path,
                                                               literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    articles = _articles(literature_fixture)
    corpus.build(conn, articles, slug="t", version="1", license="CC-BY")

    changed = articles[0]
    changed.title = "A retitled synthetic finding."
    changed.journal = "A Different Synthetic Journal"
    corpus.build(conn, [changed], slug="t", version="1", license="CC-BY-NC")

    row = conn.execute(
        "SELECT title, journal, license FROM article WHERE pmid = ?",
        (changed.pmid,)).fetchone()
    assert row["title"] == "A retitled synthetic finding."
    assert row["journal"] == "A Different Synthetic Journal"
    assert row["license"] == "CC-BY-NC"
    conn.close()


def test_coverage_reports_what_the_corpus_holds(tmp_path, literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, _articles(literature_fixture),
                 slug="cardiometabolic", version="2026.02", license="CC-BY")

    report = corpus.coverage(conn)
    assert report["article_count"] > 0
    assert "cardiometabolic@2026.02" in report["packs"]
    assert report["topics"]           # MeSH terms, most common first
    assert report["tiers"]            # counts per tier
    assert report["year_range"][0] <= report["year_range"][1]
    conn.close()


def test_coverage_on_an_empty_corpus_says_so_without_raising(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    report = corpus.coverage(conn)
    assert report["article_count"] == 0
    assert report["packs"] == []
    conn.close()


def test_retracted_articles_are_kept_and_flagged(tmp_path, literature_fixture):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, _articles(literature_fixture),
                 slug="t", version="1", license="CC-BY")

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM article WHERE retracted = 1").fetchone()["n"]
    assert n == 1, "the fixture carries exactly one retracted article"
    conn.close()


def test_embedding_the_corpus_marks_chunks_and_is_resumable(tmp_path,
                                                            literature_fixture):
    from health_agent.embeddings import HashingEmbedder
    from health_agent.literature import embed
    from health_agent.store import vector_store

    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, _articles(literature_fixture),
                 slug="t", version="1", license="CC-BY")

    store = vector_store.VectorStore(tmp_path / "vectors",
                                     table_name="literature_chunks")
    embedder = HashingEmbedder()
    first = embed.embed_corpus(conn, store, embedder)
    assert first > 0
    assert embed.embed_corpus(conn, store, embedder) == 0  # nothing pending
    conn.close()
