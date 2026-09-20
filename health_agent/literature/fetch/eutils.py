"""NCBI E-utilities for pack builds: esearch with the history server, then
efetch in pages. Called only by `literature build-pack`.

The polite delay between pages is NCBI's stated limit for callers without an
API key (three calls a second). With `NCBI_API_KEY` in the environment
the key is sent and the limit is higher, but the delay is kept: a pack build
is a one-off maintainer job, not a race.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from ..medline import MedlineParseError, ParsedArticle, parse_articles
from . import client

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
POLITE_DELAY_SECONDS = 0.4


@dataclass(frozen=True)
class SearchHandle:
    """What esearch hands back with `usehistory=y`: the hit count and the
    history-server cookie that efetch pages against, so the query is sent
    once rather than once per page."""

    count: int
    webenv: str
    query_key: str


def _params(**kw) -> dict:
    params = {"db": "pubmed", "tool": "health-agent", **kw}
    key = os.environ.get("NCBI_API_KEY")
    if key:
        params["api_key"] = key
    return params


def search(term: str, *, sort: str | None = None,
           get=client.get) -> SearchHandle:
    """Run the query on the history server and return the handle, not the
    IDs: `retmax=0` because the IDs are never needed client-side.

    `sort` matters only when a cap follows: efetch pages the handle in the
    order esearch stored it, so a capped pack takes the first N of whatever
    order that was. PubMed's default is relevance for a term like ours;
    `pub_date` makes "the first 2,000" mean the most recent 2,000.
    """
    params = _params(term=term, retmax=0, usehistory="y")
    if sort:
        params["sort"] = sort
    url = client.query_url(BASE + "esearch.fcgi", params)
    body = get(url).decode("utf-8", errors="replace")
    count = re.search(r"<Count>(\d+)</Count>", body)
    webenv = re.search(r"<WebEnv>([^<]+)</WebEnv>", body)
    key = re.search(r"<QueryKey>(\d+)</QueryKey>", body)
    if not (count and webenv and key):
        # NCBI answers a bad request with HTTP 200 and an <ERROR> element, so
        # its own explanation is the useful part of the message when present.
        error = re.search(r"<ERROR>([^<]*)</ERROR>", body)
        detail = f": {error.group(1).strip()}" if error else ""
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", None,
                                "esearch response had no Count/WebEnv/QueryKey"
                                + detail)
    return SearchHandle(int(count.group(1)), webenv.group(1), key.group(1))


def fetch_all(handle: SearchHandle, *, max_articles: int | None,
              get=client.get, sleep=time.sleep, batch: int = 500,
              progress=None) -> list[ParsedArticle]:
    """Page through the history handle. Unique PMIDs in first-seen order.

    Deduplicated here because NCBI's paging is not guaranteed stable across
    calls, and a pack that lists the same PMID twice would fail the
    corpus's uniqueness constraint at install time, long after the build.

    A page that is not MEDLINE XML (an HTML error page served with a 200,
    a truncated body) surfaces as a `FetchError` naming the page and the
    parse failure, so `build-pack` reports it like any other fetch problem
    instead of a traceback from the parser.
    """
    total = handle.count if max_articles is None else min(handle.count, max_articles)
    seen: set[str] = set()
    out: list[ParsedArticle] = []
    start = 0
    while start < total:
        size = min(batch, total - start)
        url = client.query_url(BASE + "efetch.fcgi", _params(
            query_key=handle.query_key, WebEnv=handle.webenv,
            retstart=start, retmax=size, retmode="xml"))
        try:
            page = parse_articles(get(url))
        except MedlineParseError as exc:
            raise client.FetchError(
                "eutils.ncbi.nlm.nih.gov", None,
                f"efetch page at retstart={start} was not MEDLINE XML: {exc}"
            ) from exc
        for article in page:
            if article.pmid not in seen:
                seen.add(article.pmid)
                out.append(article)
        start += size
        if progress is not None:
            progress(min(start, total), total)
        if start < total:
            sleep(POLITE_DELAY_SECONDS)
    return out
