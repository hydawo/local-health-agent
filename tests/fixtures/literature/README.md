# Literature fixture corpus

`corpus.xml` is a `PubmedArticleSet` of eight **synthetic** articles, written
for this repository. No real MEDLINE record is redistributed here — every
title, abstract, journal name, and DOI in this file is invented. Redistributing
real MEDLINE abstracts in a public repository is an unsettled licensing
question, so this project sidesteps it entirely by testing against fabricated
records instead, the same way `offline_check` runs on synthetic fixtures so
that reproducing its proof never requires real data.

The XML element shape mirrors a real PubMed `efetch` response (`MedlineCitation`,
`Article`, `PublicationTypeList`, `MeshHeadingList`, `CommentsCorrectionsList`,
`PubmedData/ArticleIdList`) so that `health_agent.literature.medline.parse_articles`
is exercised against the real shape of the data it will actually receive — only
the content of each record is made up.

As with every other fixture in this directory, no real health data is
involved.
