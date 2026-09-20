"""NCBI E-utilities for pack builds: esearch with the history server, then
efetch in pages. Called only by `literature build-pack`.

The polite delay between pages is NCBI's stated limit for callers without an
API key (three requests a second). With `NCBI_API_KEY` in the environment
the key is sent and the limit is higher, but the delay is kept: a pack build
is a one-off maintainer job, not a race.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from ..medline import ParsedArticle, parse_articles
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


def search(term: str, *, get=client.get) -> SearchHandle:
    """Run the query on the history server and return the handle, not the
    IDs: `retmax=0` because the IDs are never needed client-side."""
    url = client.query_url(BASE + "esearch.fcgi",
                           _params(term=term, retmax=0, usehistory="y"))
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
    requests, and a pack that lists the same PMID twice would fail the
    corpus's uniqueness constraint at install time, long after the build.
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
        for article in parse_articles(get(url)):
            if article.pmid not in seen:
                seen.add(article.pmid)
                out.append(article)
        start += size
        if progress is not None:
            progress(min(start, total), total)
        if start < total:
            sleep(POLITE_DELAY_SECONDS)
    return out
