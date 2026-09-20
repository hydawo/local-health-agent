# Literature packs: distributable, single-topic, abstract-only

Slice 1 of literature grounding ([2026-08-18 design](2026-08-18-literature-grounding-design.md))
built retrieval and deliberately left out acquisition. Today a person who
clones the repo has a `search_medical_literature` tool that says "no corpus
installed" until they produce a MEDLINE XML export themselves, which nobody
outside NCBI-savvy users can do. The tool's headline feature is invisible
to a tester.

The measurements in the [follow-ups design](2026-09-15-literature-follow-ups-design.md)
settled the shape: an unbounded fetch is 415k articles and 6 GB; the
meta-analysis/systematic-review/guideline slice is ~24k and, at schema v2
sizes, a few hundred megabytes. Packs are a requirement, not a preference.
And with intake retired ([ROADMAP #2](../../../ROADMAP.md)), the only
privacy question left in #2a is what a fetch request discloses, which packs
answer: a category, not a person.

This document specifies the packs, the one network module that moves them,
and the consent and threat-model changes that come with the first network
code the corpus has ever had.

## 1. Five packs, scoped to one area each

A pack is what a person chooses to install, so it maps to something they
recognise, not to a query bundle that was convenient to measure.

| Slug | Covers | Why it exists |
|---|---|---|
| `sample` | ~2,000 recent papers spanning the four areas below | A first try in under a minute, and a reproducible eval bed: run 4 was against a scratch corpus nobody else can rebuild |
| `cardiovascular` | blood pressure, cholesterol, HDL, LDL, cardiovascular disease | The lab panels and HealthKit metrics the tool already holds |
| `metabolic` | blood glucose, HbA1c, type 2 diabetes, obesity, thyroid | Same |
| `sleep` | sleep duration, quality, disorders | HealthKit sleep stages |
| `exercise` | physical activity, fitness, training, recovery | HealthKit workouts |

**Packs stay at the body-system level, never the condition level.** The
download is itself a disclosure: which pack a person fetches is visible to
GitHub and to anyone watching the network. `cardiovascular` says almost
nothing about a person. A `type-2-diabetes` pack would say a lot. This is
the line, and adding a pack below it is a design change, not a catalog
edit.

**Each pack is independent.** Installable alone, no dependency on another,
no "base" pack. The four topical packs use the evidence slice that keeps
them shippable: `Meta-Analysis`, `Systematic Review`, `Practice Guideline`,
`Guideline`, and `Randomized Controlled Trial`, 2015 onward, abstract
present. `sample` uses no type filter (it exists to show the tiers as they
really are, `unknown` included) and is capped at 2,000 newest.

Sizes are measured by the build, not estimated here; the README states
each pack's size from the manifest once built.

### The catalog is code

`health_agent/literature/packs.py` holds the catalog: for each slug, a
title, a one-line description, the MeSH query string, the evidence filter,
and the cap. The same entry drives the maintainer's build and the user's
install, so the two cannot describe different packs. Adding a pack is a
catalog entry plus a release build; changing a query is a new pack
version.

## 2. Pack format: parsed articles, not raw XML

A pack file is `<slug>-<version>.jsonl.gz`. The first line is the
manifest; every following line is one article as `medline.ParsedArticle`
serialised to JSON (pmid, title, abstract, doi, journal, pub_year,
publication_types verbatim, mesh_terms, major_terms, evidence_tier,
evidence_rank, tier_source, retracted, retraction_note).

Why parsed rather than the MEDLINE XML the `build` command reads:

- **Size.** Raw MEDLINE XML runs ~25 KB per article (author lists,
  affiliations, grant numbers, history dates); the parsed record is ~3 KB.
  For the 24k slice that is the difference between a ~600 MB and a ~75 MB
  download before compression.
- **Nothing is lost that the tool uses.** `publication_types` is kept
  verbatim so tiering stays auditable against the source, as the schema
  requires; a tier-mapping change is applied at install by re-running
  `tiers.resolve` over the stored types, not by trusting the pack's tier.
- **The parser is the maintainer's problem.** A parser fix means the
  maintainer rebuilds and re-releases; a pack version is the unit of
  reproducibility. That is the same lineage rule the schema's `pack`
  table was designed for.

The manifest:

```json
{"format": 1, "slug": "sleep", "version": "2026.09", "title": "...",
 "description": "...", "built_at": "...", "article_count": 4120,
 "query": "...", "evidence_filter": [...], "since_year": 2015,
 "source": "PubMed/MEDLINE via NCBI E-utilities", "license": "...",
 "sha256_of_articles": "..."}
```

`license` is the per-article value the installer writes into
`article.license` for every row of the pack, and it names the terms
abstracts and MEDLINE metadata are reused under (NLM's, not a Creative
Commons licence: these packs carry no full text). `LICENSES.md` gains a
section saying exactly this. The release also carries
`<slug>-<version>.sha256`; the installer verifies it before writing a row.

## 3. One network module

`health_agent/literature/fetch/` is the package the original design
reserved, and it holds the corpus's entire network surface in one file:

```
health_agent/literature/fetch/
    __init__.py
    client.py    # the ONLY file under literature/ that imports urllib
    eutils.py    # esearch + efetch over client.get; parses via medline.py
    packs.py     # download a release asset over client.get; verify; unpack
```

`client.py` exposes `get(url, *, timeout) -> bytes` and nothing else. It
sets a User-Agent naming the tool and version, follows no redirects off
the two allowed hosts, and raises a typed `FetchError` with the host and
status. `eutils.py` and `packs.py` never import `urllib`; they import
`client`. The two allowed hosts are constants in `client.py`:
`eutils.ncbi.nlm.nih.gov` and `github.com` (plus its
`objects.githubusercontent.com` redirect target for release assets). A
URL to any other host is refused before a socket opens.

**Claim 3 in `THREAT_MODEL.md` becomes three modules**, and
`test_the_network_surface_is_exactly_two_modules` becomes
`..._exactly_three_modules`, asserting the set
`{ollama_client.py, agent/backends.py, literature/fetch/client.py}`.
`test_the_query_path_cannot_reach_a_fetcher` keeps its import-graph
assertion unchanged (nothing reachable from the ask path may import
`literature.fetch`) and its text scan learns that `literature/fetch/client.py`
is the one file allowed a transport import. The scan should now also
assert that `eutils.py` and `packs.py` do *not* import a transport, so the
single-module rule is enforced inside the package too.

`offline-check` is untouched: it never imports `fetch`, and the
import-graph test proves the ask path cannot either.

## 4. Three commands

```
health-agent literature packs                       # list the catalog, installed state, sizes
health-agent literature install <slug> [--version V] [--from <file|url>] [--no-embed]
health-agent literature build-pack <slug> --out <dir> [--version V] [--max N]   # maintainer
```

**`packs`** reads the catalog and the local `pack` table. No network.

**`install`** is the user-facing network command. It: shows the consent
notice (below) and stops unless accepted; downloads `<slug>-<version>.jsonl.gz`
and its `.sha256` from the release URL derived from the catalog and a
`PACK_RELEASE_TAG` constant; verifies the digest; then hands the articles
to `corpus.build` exactly as `literature build` does, re-resolving tiers
from `publication_types`, and embeds unless `--no-embed`. `--from` takes a
local file or an explicit URL, which is how a pack built on one machine is
tested on another without a release, and how a tester behind a proxy
installs by hand. `--version` defaults to the newest the catalog names.
Installing a pack already present at the same version is a no-op that
says so; a newer version replaces the older one's rows (the `pack` row's
`version` changes, articles are upserted, articles no longer in the pack
are removed).

**`build-pack`** is the maintainer command and the E-utilities client's
only caller. It runs esearch with `usehistory=y`, pages efetch in batches
of 500 with the polite delay NCBI asks for (three requests per second
without an API key; the command reads `NCBI_API_KEY` from the environment
if set and says nothing else about it), parses with `medline.parse_articles`
as it streams, and writes the pack file plus `.sha256` to `--out`. `--max`
caps the count (the `sample` catalog entry sets 2,000 by default). It never
touches the local index. Publishing the files to a GitHub Release is a
manual step, documented in `CONTRIBUTING.md`, not a command: the tool
should not hold a GitHub token.

## 5. Cross-pack deduplication: schema v4

An "exercise and hypertension" paper belongs in both `exercise` and
`cardiovascular`. Today `article` is unique per `(pack_id, pmid)`, so
installing both would store it twice and `search_medical_literature` would
return the same finding twice under two pack names. Schema v4:

- `article.pack_id` goes away; `article.pmid` becomes globally unique.
- New table `article_pack(article_id, pack_id)`, primary key on the pair,
  `ON DELETE CASCADE` from both sides.
- `pack.article_count` is the count of that pack's rows in `article_pack`.
- Install upserts the article by pmid (every mutable column refreshed, as
  `build` already does) and inserts the `article_pack` link. Removing a
  pack deletes its links and then any article with no remaining link.
- `coverage()["packs"]` is unchanged; `search` results gain nothing (a
  finding is an article, not an article-in-a-pack). `literature status`
  gains a line for articles shared between packs, so the dedupe is visible.

`LITERATURE_SCHEMA_VERSION = 4`. The existing `--rebuild` path and the
version check on open already handle the migration by refusing v3 and
naming the flag. `literature build --from <xml>` keeps working and creates
or updates a pack named by `--slug` exactly as before, through the same
link table.

## 6. Consent, and what leaves the machine

The cloud tier's consent mechanism (`health_agent/consent.py`) is reused
with a second notice rather than a second mechanism: `consent.py` gains a
`Notice` with its own version and filename, and the existing functions take
which notice they are recording. `literature_consent.json` sits beside
`cloud_consent.json` under the index directory, per data folder, for the
same reason the cloud one does.

The literature notice says, in full and before the first `install` or
`build-pack`:

- what is sent: for `install`, a request to `github.com` for a named pack
  file (the pack name, your IP address, and the tool's version string;
  nothing from your data folder, nothing about your questions); for
  `build-pack`, a search query built from the catalog's MeSH terms to
  `eutils.ncbi.nlm.nih.gov` (the terms, your IP, the tool's version;
  nothing else);
- when: only when you run one of those two commands, never during `ask`,
  `ingest`, or anything else, and `offline-check` still proves that;
- that the category you download is visible to the host, which is why
  packs are broad;
- how to revoke.

`NOTICE_VERSION` for the literature notice starts at 1 and bumps when what
is sent changes. `health-agent literature-consent --revoke` mirrors the
cloud command.

`THREAT_MODEL.md` gains **Claim 6: literature packs**, stating the above,
the two hosts, the single module, and the verification commands (`grep`
for transport imports under `literature/`, and the three-module test).

## 7. Documentation

- `README.md`: the corpus section's first paragraph becomes "install a
  pack" with the five slugs and a sentence on what a download reveals; the
  `literature build --from` path stays as the way to use your own export.
  The "known hole" bullet "Slice 1 has no network fetch" is replaced.
- `ROADMAP.md`: #1a is done in this shape; #2a's remaining question is
  answered by packs; both say so and point here. #6 (corpus versioning)
  notes that pack versions are now the lineage unit.
- `LICENSES.md`: a "Packs" section, as in §2.
- `CONTRIBUTING.md`: how a maintainer builds and publishes a pack, and
  the rule that new packs stay at the body-system level.

## 8. Testing

Nothing in the suite opens a socket. `client.get` is the one seam, and
tests replace it.

- `tests/test_literature_packs_format.py`: manifest round-trip; an article
  serialised and re-read equals the `ParsedArticle`; the sha256 covers the
  article lines and not the manifest.
- `tests/test_literature_eutils.py`: esearch/efetch response parsing from
  committed fixture XML (small, synthetic, in the existing fixture style);
  batching arithmetic; the polite delay is called between batches; a
  non-200 raises `FetchError` with the host.
- `tests/test_literature_install.py`: `install --from <local file>` builds
  a corpus, resolves tiers from `publication_types` rather than trusting
  the pack, records the license per row, embeds; a bad digest refuses
  before any row is written; same-version reinstall is a no-op; a newer
  version replaces; two packs sharing a PMID store it once and link it
  twice; removing one pack keeps the shared article.
- `tests/test_no_network.py`: the three-module assertion; the text scan
  with the single-file exemption; the ask-path import-graph assertion
  unchanged and still passing with `fetch/` present.
- `tests/test_cli.py`: `packs` lists the catalog offline; `install`
  without consent stops before any fetch (the seam records zero calls);
  `--from` a local file needs consent too (it is the same command, and a
  URL can be passed there).
- `tests/test_literature_schema.py`: v4, `article_pack` present,
  `article.pmid` unique, `pack_id` gone from `article`.
- `offline-check`: unchanged, still PASS.
- After the branch: build the `sample` pack for real with `build-pack`
  (one network run, by the maintainer, with consent), install it into the
  eval index, and re-run the 27 questions as run 5. That run is the first
  one anyone can reproduce.

## Not in scope

Full text, ClinicalTrials.gov, automatic updates or update checks (a
person runs `install` again; the tool never phones out on its own), pack
signing beyond sha256, and a pack for any area the tool holds no data on.
