"""Building a corpus from parsed articles."""
from __future__ import annotations

import pytest

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
    assert report["topics"]           # major MeSH topics, most common first
    assert report["topics_from"] == "major_topics"
    assert report["tiers"]            # counts per tier
    assert report["year_range"][0] <= report["year_range"][1]
    conn.close()


def test_coverage_counts_major_topics_not_every_heading(tmp_path):
    """On 2,000 real abstracts the most common headings were Humans, Male,
    Female, Middle Aged, Adult, Aged: population check tags, not what the
    corpus is about. MEDLINE marks the subject headings itself
    (MajorTopicYN="Y"), so coverage() reads that and infers nothing."""
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    articles = [
        medline.ParsedArticle(pmid=str(i), title="t", abstract="body",
                              mesh_terms=["Humans", "Female", "Insomnia"],
                              major_terms=["Insomnia"])
        for i in range(3)
    ]
    articles.append(medline.ParsedArticle(
        pmid="9", title="t", abstract="body",
        mesh_terms=["Humans", "Male", "Gout"], major_terms=["Gout"]))
    corpus.build(conn, articles, slug="t", version="1", license="CC-BY")

    report = corpus.coverage(conn)
    assert report["topics"] == ["Insomnia", "Gout"]
    assert report["topics_from"] == "major_topics"
    assert report["mesh_terms"] == 5
    assert report["major_topics"] == 2
    conn.close()


def test_coverage_falls_back_to_every_heading_when_none_is_major(tmp_path,
                                                                literature_fixture):
    """An export with no MajorTopicYN attributes at all (older exports, or a
    hand-built one) would otherwise report no topics for a corpus that has
    plenty. Falling back to all headings is the only honest alternative;
    it is labelled so a reader can tell which list they are looking at."""
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    articles = _articles(literature_fixture)
    for article in articles:
        article.major_terms = []
    corpus.build(conn, articles, slug="t", version="1", license="CC-BY")

    report = corpus.coverage(conn)
    assert report["topics_from"] == "all_mesh_terms"
    assert report["major_topics"] == 0
    assert report["topics"] == [
        "Cholesterol", "Hypertension", "LDL", "Exercise", "Heart Rate", "Sleep"]
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


def _one(pmid: str, title: str = "t") -> medline.ParsedArticle:
    return medline.ParsedArticle(pmid=pmid, title=title, abstract="Some abstract text.",
                                 publication_types=["Randomized Controlled Trial"],
                                 evidence_tier="rct", evidence_rank=3,
                                 tier_source="publication_type")


def test_two_packs_sharing_a_pmid_store_it_once(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="exercise", version="1", license="x")
    corpus.build(conn, [_one("2"), _one("3")], slug="cardiovascular", version="1", license="x")

    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 3
    links = conn.execute("SELECT COUNT(*) AS n FROM article_pack").fetchone()["n"]
    assert links == 4
    counts = {r["slug"]: r["article_count"] for r in conn.execute("SELECT slug, article_count FROM pack")}
    assert counts == {"exercise": 2, "cardiovascular": 2}
    assert corpus.coverage(conn)["shared_articles"] == 1
    conn.close()


def test_rebuilding_a_pack_unlinks_articles_it_no_longer_holds(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="sleep", version="1", license="x")
    corpus.build(conn, [_one("2")], slug="sleep", version="1", license="x")
    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 1
    assert conn.execute("SELECT pmid FROM article").fetchone()["pmid"] == "2"
    conn.close()


def test_removing_a_pack_keeps_articles_another_pack_still_links(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="exercise", version="1", license="x")
    corpus.build(conn, [_one("2")], slug="cardiovascular", version="1", license="x")
    removed = corpus.remove_pack(conn, "exercise")
    assert removed == 1
    pmids = {r["pmid"] for r in conn.execute("SELECT pmid FROM article")}
    assert pmids == {"2"}
    assert [r["slug"] for r in conn.execute("SELECT slug FROM pack")] == ["cardiovascular"]
    conn.close()


def test_a_newer_pack_version_replaces_the_older_one(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1")], slug="sleep", version="2026.09", license="x")
    corpus.build(conn, [_one("1"), _one("2")], slug="sleep", version="2026.10", license="x")
    packs = [(r["slug"], r["version"]) for r in conn.execute("SELECT slug, version FROM pack")]
    assert packs == [("sleep", "2026.10")]
    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 2
    conn.close()


def _fresh(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    return conn


def _art(pmid, title="T", abstract="Some abstract text.", **kw):
    return medline.ParsedArticle(pmid=pmid, title=title, abstract=abstract,
                                 publication_types=["Journal Article"], **kw)


def test_add_links_new_and_existing_articles_and_unlinks_nothing(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1"), _art("2")], slug="sleep", version="2026.09",
                 license="L")
    before = conn.execute("SELECT version, built_at FROM pack").fetchone()

    stats = corpus.add(conn, [_art("2", title="T2 revised"), _art("3")],
                       slug="sleep", license="L", window_from="2026-09-19",
                       window_to="2026-10-04", matched=2)

    assert (stats.articles, stats.added) == (2, 1)
    linked = {r["pmid"] for r in conn.execute(
        "SELECT a.pmid FROM article a JOIN article_pack ap ON ap.article_id = a.id")}
    assert linked == {"1", "2", "3"}          # 1 stayed linked
    assert conn.execute("SELECT title FROM article WHERE pmid = '2'").fetchone()[0] == "T2 revised"
    pack = conn.execute("SELECT version, built_at, refreshed_at, article_count FROM pack").fetchone()
    assert (pack["version"], pack["built_at"]) == (before["version"], before["built_at"])
    assert pack["refreshed_at"] is not None
    assert pack["article_count"] == 3
    log = conn.execute("SELECT * FROM refresh_log").fetchone()
    assert (log["window_from"], log["window_to"], log["matched"], log["added"], log["retracted"]) == \
        ("2026-09-19", "2026-10-04", 2, 1, 0)


def test_add_requires_an_installed_pack(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(corpus.PackNotInstalled):
        corpus.add(conn, [_art("1")], slug="sleep", license="L",
                   window_from="a", window_to="b", matched=1)


def test_mark_retracted_flips_only_rows_that_exist(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1"), _art("2")], slug="sleep", version="1", license="L")
    n = corpus.mark_retracted(conn, {"2": "Retraction in: J 2026", "9": None})
    assert n == 1
    rows = {r["pmid"]: (r["retracted"], r["retraction_note"]) for r in
            conn.execute("SELECT pmid, retracted, retraction_note FROM article")}
    assert rows["2"] == (1, "Retraction in: J 2026")
    assert rows["1"] == (0, None)
    # idempotent: already-retracted rows are not counted again
    assert corpus.mark_retracted(conn, {"2": "Retraction in: J 2026"}) == 0


def test_coverage_reports_refresh_per_pack(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1")], slug="sleep", version="1", license="L")
    report = corpus.coverage(conn)
    (row,) = report["pack_rows"]
    assert row["slug"] == "sleep" and row["refreshed_at"] is None
    corpus.add(conn, [_art("2")], slug="sleep", license="L",
               window_from="a", window_to="b", matched=1)
    assert corpus.coverage(conn)["pack_rows"][0]["refreshed_at"] is not None
