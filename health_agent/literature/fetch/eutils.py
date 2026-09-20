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
# NCBI refuses to page a PubMed result past this record (HTTP 400 at
# retstart=10000), whatever the history server holds. The first attempt at
# the cardiovascular pack (79,280 matches) died there. A result set larger
# than this is fetched as date slices, each under the cap, which is the
# route NCBI's own documentation points at.
PAGE_LIMIT = 10_000


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
           mindate: str | None = None, maxdate: str | None = None,
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
    if mindate or maxdate:
        # Both are required by esearch; publication date, as YYYY/MM/DD.
        params.update(datetype="pdat", mindate=mindate, maxdate=maxdate)
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


def _month_slices(year: int) -> list[tuple[str, str]]:
    import calendar
    return [(f"{year}/{m:02d}/01", f"{year}/{m:02d}/{calendar.monthrange(year, m)[1]:02d}")
            for m in range(1, 13)]


def search_slices(term: str, *, first_year: int, last_year: int,
                  get=client.get, sleep=time.sleep) -> list[SearchHandle]:
    """Handles whose counts each fit under `PAGE_LIMIT`.

    One handle when the whole result does. Otherwise one per publication
    year from `first_year` to `last_year`, and a year that still exceeds
    the cap is split into months. A month over 10,000 matching abstracts
    on one body system would be a different problem; it raises rather
    than silently truncating.
    """
    whole = search(term, get=get)
    if whole.count <= PAGE_LIMIT:
        return [whole]
    handles: list[SearchHandle] = []
    for year in range(first_year, last_year + 1):
        sleep(POLITE_DELAY_SECONDS)
        by_year = search(term, mindate=f"{year}/01/01", maxdate=f"{year}/12/31", get=get)
        if by_year.count == 0:
            continue
        if by_year.count <= PAGE_LIMIT:
            handles.append(by_year)
            continue
        for lo, hi in _month_slices(year):
            sleep(POLITE_DELAY_SECONDS)
            by_month = search(term, mindate=lo, maxdate=hi, get=get)
            if by_month.count > PAGE_LIMIT:
                raise client.FetchError(
                    "eutils.ncbi.nlm.nih.gov", None,
                    f"{by_month.count} matches in {lo}..{hi} exceed NCBI's "
                    f"{PAGE_LIMIT}-record page limit")
            if by_month.count:
                handles.append(by_month)
    return handles


def fetch_term(term: str, *, sort: str | None, max_articles: int | None,
               first_year: int | None, get=client.get, sleep=time.sleep,
               progress=None) -> tuple[int, list[ParsedArticle]]:
    """(matching count, articles) for a pack's term, paging within NCBI's
    limit. A capped pack (`max_articles` under the limit) keeps its sort
    and takes one handle, so "the first N" means what the catalog says."""
    if max_articles is not None and max_articles <= PAGE_LIMIT:
        handle = search(term, sort=sort, get=get)
        return handle.count, fetch_all(handle, max_articles=max_articles,
                                       get=get, sleep=sleep, progress=progress)
    import datetime
    handles = search_slices(term, first_year=first_year or 1900,
                            last_year=datetime.date.today().year + 1,
                            get=get, sleep=sleep)
    total = sum(h.count for h in handles)
    if max_articles is not None:
        total = min(total, max_articles)
    seen: set[str] = set()
    out: list[ParsedArticle] = []
    done = 0
    for handle in handles:
        remaining = None if max_articles is None else max_articles - len(out)
        if remaining is not None and remaining <= 0:
            break
        page = fetch_all(handle, max_articles=remaining, get=get, sleep=sleep,
                         progress=(lambda d, t, base=done: progress(min(base + d, total), total))
                         if progress else None)
        for article in page:
            if article.pmid not in seen:
                seen.add(article.pmid)
                out.append(article)
        done += handle.count
        sleep(POLITE_DELAY_SECONDS)
    return total, out
