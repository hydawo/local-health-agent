"""Pack files: the catalog is code, the file is parsed articles, digests hold."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from health_agent.literature import medline, packs


def _articles():
    return [
        medline.ParsedArticle(pmid="1", title="A", abstract="Alpha text.",
                              journal="J", pub_year=2024, doi="10.1/a",
                              publication_types=["Journal Article", "Meta-Analysis"],
                              mesh_terms=["Sleep", "Humans"], major_terms=["Sleep"],
                              evidence_tier="meta_analysis", evidence_rank=1,
                              tier_source="publication_type"),
        medline.ParsedArticle(pmid="2", title="B", abstract="Beta text.",
                              publication_types=["Journal Article"],
                              retracted=True, retraction_note="withdrawn"),
    ]


def test_catalog_holds_exactly_the_five_body_system_packs():
    assert set(packs.CATALOG) == {"sample", "cardiovascular", "metabolic", "sleep", "exercise"}
    for spec in packs.CATALOG.values():
        assert spec.query and spec.title and spec.description
    assert packs.CATALOG["sample"].max_articles == 2000
    assert packs.CATALOG["sample"].evidence_filter == ()
    for slug in ("cardiovascular", "metabolic", "sleep", "exercise"):
        assert "Meta-Analysis" in packs.CATALOG[slug].evidence_filter
        assert packs.CATALOG[slug].since_year == 2015


def test_write_then_read_round_trips_articles_and_manifest(tmp_path):
    path = tmp_path / packs.asset_name("sleep", "2026.09")
    manifest = packs.write_pack(path, packs.CATALOG["sleep"], "2026.09", _articles(),
                                license=packs.PACK_LICENSE)
    assert path.name == "sleep-2026.09.jsonl.gz"
    assert manifest.article_count == 2
    assert manifest.slug == "sleep" and manifest.format == 1

    read_manifest, articles = packs.read_pack(path)
    assert read_manifest == manifest
    assert articles == _articles()


def test_first_line_is_the_manifest_and_the_digest_covers_the_articles(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    manifest = packs.write_pack(path, packs.CATALOG["sleep"], "1", _articles(),
                                license="L")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        first = json.loads(fh.readline())
        rest = fh.read()
    assert first["format"] == 1 and first["slug"] == "sleep"
    assert manifest.sha256_of_articles == packs.sha256_text(rest)


def test_read_refuses_a_pack_whose_article_digest_does_not_match(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    packs.write_pack(path, packs.CATALOG["sleep"], "1", _articles(), license="L")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    lines[1] = lines[1].replace("Alpha text.", "Tampered text.")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with pytest.raises(packs.PackError):
        packs.read_pack(path)


def test_read_refuses_an_unknown_format_version(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"format": 99, "slug": "sleep"}) + "\n")
    with pytest.raises(packs.PackError):
        packs.read_pack(path)


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    assert packs.sha256_file(p) == hashlib.sha256(b"abc").hexdigest()
