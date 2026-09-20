"""Parsing MEDLINE XML. No network: bytes in, dataclasses out."""
from __future__ import annotations

from health_agent.literature import medline

SAMPLE = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>30000001</PMID>
   <Article>
    <Journal>
     <JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue>
     <Title>Cochrane Database Syst Rev</Title>
    </Journal>
    <ArticleTitle>Exercise for lowering blood pressure.</ArticleTitle>
    <Abstract>
     <AbstractText Label="BACKGROUND">Blood pressure matters.</AbstractText>
     <AbstractText Label="RESULTS">Aerobic exercise reduced SBP.</AbstractText>
    </Abstract>
    <PublicationTypeList>
     <PublicationType>Journal Article</PublicationType>
     <PublicationType>Meta-Analysis</PublicationType>
    </PublicationTypeList>
   </Article>
   <MeshHeadingList>
    <MeshHeading><DescriptorName MajorTopicYN="Y">Hypertension</DescriptorName></MeshHeading>
    <MeshHeading><DescriptorName MajorTopicYN="N">Exercise</DescriptorName></MeshHeading>
    <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
   </MeshHeadingList>
   <CommentsCorrectionsList>
    <CommentsCorrections RefType="Cites"><PMID>111</PMID></CommentsCorrections>
   </CommentsCorrectionsList>
  </MedlineCitation>
  <PubmedData>
   <ArticleIdList>
    <ArticleId IdType="doi">10.1002/cochrane.1</ArticleId>
   </ArticleIdList>
  </PubmedData>
 </PubmedArticle>
</PubmedArticleSet>
"""

RETRACTED = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>30000002</PMID>
   <Article>
    <ArticleTitle>A study later withdrawn.</ArticleTitle>
    <Abstract><AbstractText>Findings.</AbstractText></Abstract>
    <PublicationTypeList>
     <PublicationType>Randomized Controlled Trial</PublicationType>
    </PublicationTypeList>
   </Article>
   <CommentsCorrectionsList>
    <CommentsCorrections RefType="RetractionIn">
     <RefSource>J Retract. 2021;1:1</RefSource>
    </CommentsCorrections>
   </CommentsCorrectionsList>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>
"""


def test_parses_core_fields():
    (article,) = medline.parse_articles(SAMPLE)
    assert article.pmid == "30000001"
    assert article.title == "Exercise for lowering blood pressure."
    assert article.journal == "Cochrane Database Syst Rev"
    assert article.pub_year == 2019
    assert article.doi == "10.1002/cochrane.1"


def test_labelled_abstract_sections_are_joined_with_their_labels():
    (article,) = medline.parse_articles(SAMPLE)
    assert "BACKGROUND: Blood pressure matters." in article.abstract
    assert "RESULTS: Aerobic exercise reduced SBP." in article.abstract


def test_tier_comes_from_publication_types():
    (article,) = medline.parse_articles(SAMPLE)
    assert article.publication_types == ["Journal Article", "Meta-Analysis"]
    assert article.evidence_tier == "meta_analysis"
    assert article.tier_source == "publication_type"


def test_mesh_terms_are_captured():
    (article,) = medline.parse_articles(SAMPLE)
    assert article.mesh_terms == ["Hypertension", "Exercise", "Humans"]


def test_major_topics_are_read_from_the_majortopicyn_attribute():
    """Only MajorTopicYN="Y" counts. An absent attribute is not a major
    topic either: older exports omit it, and treating absence as "Y" would
    put every check tag (Humans, Male, Female) back into the topic list."""
    (article,) = medline.parse_articles(SAMPLE)
    assert article.major_terms == ["Hypertension"]


def test_a_record_without_major_flags_has_no_major_terms():
    """An export that omits the attribute yields terms and no majors;
    corpus.coverage() handles that case by falling back to every term."""
    unflagged = SAMPLE.replace(b' MajorTopicYN="Y"', b"").replace(
        b' MajorTopicYN="N"', b"")
    assert b"MajorTopicYN" not in unflagged
    (article,) = medline.parse_articles(unflagged)
    assert article.mesh_terms == ["Hypertension", "Exercise", "Humans"]
    assert article.major_terms == []


def test_retraction_is_detected_from_comments_corrections():
    (article,) = medline.parse_articles(RETRACTED)
    assert article.retracted is True
    assert "J Retract" in article.retraction_note


def test_unrelated_comments_corrections_do_not_mark_a_retraction():
    (article,) = medline.parse_articles(SAMPLE)
    assert article.retracted is False


def test_article_without_pmid_is_skipped_not_crashed():
    xml = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation>
    <Article><ArticleTitle>No id</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    assert medline.parse_articles(xml) == []


def test_malformed_xml_raises_a_typed_error():
    import pytest

    with pytest.raises(medline.MedlineParseError):
        medline.parse_articles(b"<PubmedArticleSet><oops>")
