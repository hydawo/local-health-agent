"""The corpus vector table stays in step with `article_chunk`.

A vector is keyed by its chunk's SQLite id. Two things used to leave vectors
behind with no chunk: every build deleted and re-inserted an unchanged
article's chunks under new ids, and no path ever removed a vector whose
chunk was gone. The stale rows were identical to live ones and ranked
alongside them, taking slots from the search's over-fetch. These tests
reproduce the reviewer's shape (two overlapping packs, a forced reinstall)
at a smaller scale.
"""
from __future__ import annotations

import pytest

from health_agent.embeddings import HashingEmbedder
from health_agent.literature import corpus, embed, medline, schema
from health_agent.store import vector_store


def _articles(prefix: str, pmids: range) -> list[medline.ParsedArticle]:
    return [medline.ParsedArticle(
                pmid=str(pmid), title=f"{prefix} {pmid}",
                abstract=f"Abstract {pmid} about {prefix}. " * 5,
                mesh_terms=[prefix])
            for pmid in pmids]


@pytest.fixture
def built(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    store = vector_store.VectorStore(tmp_path / "vectors",
                                     table_name=embed.TABLE_NAME)
    yield conn, store, HashingEmbedder()
    conn.close()


def _install(conn, store, embedder, articles, slug):
    corpus.build(conn, articles, slug=slug, version="1", license="L")
    embed.embed_corpus(conn, store, embedder)
    return embed.reclaim_orphans(conn, store)


def _live_chunk_ids(conn) -> set[int]:
    return {int(r["id"]) for r in conn.execute("SELECT id FROM article_chunk")}


def test_vector_table_matches_live_chunks_across_overlapping_installs(built):
    """Pack A holds 10 articles, pack B 10 with 3 shared; A is then
    reinstalled. After each step the vector table holds exactly the live
    chunks: nothing stale, nothing missing."""
    conn, store, embedder = built
    pack_a = _articles("a", range(1, 11))
    pack_b = _articles("b", range(8, 18))
    # The three shared PMIDs carry the same text in both packs, as a
    # shared article would in the real catalog.
    for shared in pack_b[:3]:
        shared.abstract = next(a for a in pack_a if a.pmid == shared.pmid).abstract

    assert _install(conn, store, embedder, pack_a, "a") == 0
    assert store.count() == len(_live_chunk_ids(conn)) == 10

    reclaimed = _install(conn, store, embedder, pack_b, "b")
    assert reclaimed == 0                       # shared text kept its chunks
    assert store.count() == len(_live_chunk_ids(conn)) == 17

    ids_before = _live_chunk_ids(conn)
    reclaimed = _install(conn, store, embedder, pack_a, "a")
    assert reclaimed == 0
    assert _live_chunk_ids(conn) == ids_before  # a reinstall rewrote nothing
    assert store.count() == 17
    assert store.chunk_ids() == ids_before


def test_a_changed_abstract_gets_new_chunks_and_its_old_vector_is_reclaimed(built):
    conn, store, embedder = built
    pack = _articles("a", range(1, 4))
    _install(conn, store, embedder, pack, "a")
    before = {r["pmid"]: r["id"] for r in conn.execute(
        "SELECT a.pmid, c.id FROM article_chunk c JOIN article a ON a.id = c.article_id")}

    pack[0].abstract = "A corrected abstract, so the vector for the old text is stale."
    reclaimed = _install(conn, store, embedder, pack, "a")
    after = {r["pmid"]: r["id"] for r in conn.execute(
        "SELECT a.pmid, c.id FROM article_chunk c JOIN article a ON a.id = c.article_id")}

    assert reclaimed == 1
    assert after["1"] != before["1"]
    assert after["2"] == before["2"] and after["3"] == before["3"]
    assert store.chunk_ids() == set(after.values())
    assert store.count() == 3


def test_removing_a_pack_leaves_vectors_the_caller_reclaims(built):
    """`remove_pack` has only the SQLite connection; the vectors of the
    articles it dropped are reclaimed by whoever holds the store."""
    conn, store, embedder = built
    _install(conn, store, embedder, _articles("a", range(1, 6)), "a")
    _install(conn, store, embedder, _articles("b", range(4, 9)), "b")
    assert store.count() == 8

    corpus.remove_pack(conn, "b")
    assert store.count() == 8                  # not yet
    assert embed.reclaim_orphans(conn, store) == 3
    assert store.count() == len(_live_chunk_ids(conn)) == 5
    assert store.chunk_ids() == _live_chunk_ids(conn)


def test_reclaim_deletes_in_batches(built, monkeypatch):
    """A `--force` reinstall of a real pack can strand thousands of ids at
    once; the delete filter is an `IN (...)` literal, so it is batched."""
    conn, store, embedder = built
    _install(conn, store, embedder, _articles("a", range(1, 1201)), "a")
    conn.execute("DELETE FROM article_chunk")
    conn.commit()

    table = store._open_table()
    filters = []
    real_delete = table.delete
    monkeypatch.setattr(table, "delete",
                        lambda where: filters.append(where) or real_delete(where))
    assert store.delete_chunks(store.chunk_ids() - _live_chunk_ids(conn),
                               batch_size=500) == 1200
    assert len(filters) == 3
    assert all(f.startswith("chunk_id IN (") for f in filters)
    assert store.count() == 0
    assert vector_store.DELETE_BATCH == 500


def test_reclaim_on_an_empty_store_is_a_no_op(built):
    conn, store, _ = built
    corpus.build(conn, _articles("a", range(1, 3)), slug="a", version="1", license="L")
    assert embed.reclaim_orphans(conn, store) == 0
