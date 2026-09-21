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
from datetime import date as _date

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
# NCBI's search backend drops the occasional request outright ("Request to
# GWSearch failed", an HTTP 5xx, a reset). One such answer cost the whole
# metabolic pack on the first build night. A transient failure is retried
# this many times with a growing pause; a refused host, a 4xx, or the page
# limit is not transient and is raised at once.
RETRIES = 3
RETRY_PAUSE_SECONDS = 5.0
_TRANSIENT_TEXT = ("search backend failed", "gwsearch", "timed out",
                   "connection reset", "remote end closed")


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


def _transient(exc: Exception) -> bool:
    if isinstance(exc, client.FetchError):
        if exc.status is not None:
            return exc.status >= 500
        return any(t in str(exc).lower() for t in _TRANSIENT_TEXT)
    return False


def with_retry(call, *, sleep=time.sleep, retries: int = RETRIES):
    """Run `call()`; on a transient NCBI failure pause and try again, up to
    `retries` more times. Anything else propagates on the first raise."""
    for attempt in range(retries + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt == retries or not _transient(exc):
                raise
            sleep(RETRY_PAUSE_SECONDS * (attempt + 1))


def search(term: str, *, sort: str | None = None,
           mindate: str | None = None, maxdate: str | None = None,
           datetype: str = "pdat", get=client.get, sleep=time.sleep) -> SearchHandle:
    """Run the query on the history server and return the handle, not the
    IDs: `retmax=0` because the IDs are never needed client-side.

    `sort` matters only when a cap follows: efetch pages the handle in the
    order esearch stored it, so a capped pack takes the first N of whatever
    order that was. PubMed's default is relevance for a term like ours;
    `pub_date` makes "the first 2,000" mean the most recent 2,000.

    `datetype` picks which NCBI date the window is measured against:
    `pdat` (publication date, the default, used for pack builds) or `edat`
    (Entrez date, the day NCBI indexed the record, used by `refresh_pack`
    so "new since my last refresh" means indexed since then).
    """
    params = _params(term=term, retmax=0, usehistory="y")
    if sort:
        params["sort"] = sort
    if mindate or maxdate:
        # Both are required by esearch; formatted as YYYY/MM/DD.
        params.update(datetype=datetype, mindate=mindate, maxdate=maxdate)
    url = client.query_url(BASE + "esearch.fcgi", params)

    def once() -> SearchHandle:
        body = get(url).decode("utf-8", errors="replace")
        count = re.search(r"<Count>(\d+)</Count>", body)
        webenv = re.search(r"<WebEnv>([^<]+)</WebEnv>", body)
        key = re.search(r"<QueryKey>(\d+)</QueryKey>", body)
        if not (count and webenv and key):
            # NCBI answers a bad request with HTTP 200 and an <ERROR>
            # element, so its own explanation is the useful part of the
            # message when present.
            error = re.search(r"<ERROR>([^<]*)</ERROR>", body)
            detail = f": {error.group(1).strip()}" if error else ""
            raise client.FetchError("eutils.ncbi.nlm.nih.gov", None,
                                    "esearch response had no Count/WebEnv/QueryKey"
                                    + detail)
        return SearchHandle(int(count.group(1)), webenv.group(1), key.group(1))

    return with_retry(once, sleep=sleep)


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
            page = parse_articles(with_retry(lambda: get(url), sleep=sleep))
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


def _month_slices(year: int) -> list[tuple[_date, _date]]:
    import calendar
    return [(_date(year, m, 1), _date(year, m, calendar.monthrange(year, m)[1]))
            for m in range(1, 13)]


def _fmt(d: _date) -> str:
    return d.strftime("%Y/%m/%d")


def _parse(s: str) -> _date:
    return _date(*(int(p) for p in s.split("/")))


def _clip(lo: _date, hi: _date, window_from: _date | None,
          window_to: _date | None) -> tuple[_date, _date] | None:
    """`(lo, hi)` narrowed to the window, or None if it falls wholly outside."""
    if window_from and hi < window_from:
        return None
    if window_to and lo > window_to:
        return None
    clipped_lo = max(lo, window_from) if window_from else lo
    clipped_hi = min(hi, window_to) if window_to else hi
    return clipped_lo, clipped_hi


def search_slices(term: str, *, first_year: int, last_year: int,
                  get=client.get, sleep=time.sleep, mindate: str | None = None,
                  maxdate: str | None = None,
                  datetype: str = "pdat") -> list[SearchHandle]:
    """Handles whose counts each fit under `PAGE_LIMIT`.

    One handle when the whole result does. Otherwise one per publication
    year from `first_year` to `last_year`, and a year that still exceeds
    the cap is split into months. A month over 10,000 matching abstracts
    on one body system would be a different problem; it raises rather
    than silently truncating.

    `mindate`/`maxdate` (with `datetype`) narrow the whole search and every
    slice to a window: a year wholly outside it is skipped, and a year or
    month partly inside it is clipped to the window's edges.
    """
    window_from = _parse(mindate) if mindate else None
    window_to = _parse(maxdate) if maxdate else None
    whole = search(term, mindate=mindate, maxdate=maxdate, datetype=datetype,
                   get=get, sleep=sleep)
    if whole.count <= PAGE_LIMIT:
        return [whole]
    handles: list[SearchHandle] = []
    for year in range(first_year, last_year + 1):
        clipped = _clip(_date(year, 1, 1), _date(year, 12, 31), window_from, window_to)
        if clipped is None:
            continue
        year_lo, year_hi = clipped
        sleep(POLITE_DELAY_SECONDS)
        by_year = search(term, mindate=_fmt(year_lo), maxdate=_fmt(year_hi),
                         datetype=datetype, get=get, sleep=sleep)
        if by_year.count == 0:
            continue
        if by_year.count <= PAGE_LIMIT:
            handles.append(by_year)
            continue
        for lo, hi in _month_slices(year):
            month_clipped = _clip(lo, hi, window_from, window_to)
            if month_clipped is None:
                continue
            month_lo, month_hi = month_clipped
            sleep(POLITE_DELAY_SECONDS)
            by_month = search(term, mindate=_fmt(month_lo), maxdate=_fmt(month_hi),
                              datetype=datetype, get=get, sleep=sleep)
            if by_month.count > PAGE_LIMIT:
                raise client.FetchError(
                    "eutils.ncbi.nlm.nih.gov", None,
                    f"{by_month.count} matches in {_fmt(month_lo)}..{_fmt(month_hi)} "
                    f"exceed NCBI's {PAGE_LIMIT}-record page limit")
            if by_month.count:
                handles.append(by_month)
    return handles


def fetch_term(term: str, *, sort: str | None, max_articles: int | None,
               first_year: int | None, get=client.get, sleep=time.sleep,
               progress=None, mindate: str | None = None,
               maxdate: str | None = None,
               datetype: str = "pdat") -> tuple[int, list[ParsedArticle]]:
    """(matching count, articles) for a pack's term, paging within NCBI's
    limit. A capped pack (`max_articles` under the limit) keeps its sort
    and takes one handle, so "the first N" means what the catalog says.

    `mindate`/`maxdate`/`datetype` narrow the search to a window (see
    `search` and `search_slices`). When a window is given and `first_year`
    is not, the slicing range is derived from the window instead of
    defaulting to "everything".
    """
    if max_articles is not None and max_articles <= PAGE_LIMIT:
        handle = search(term, sort=sort, mindate=mindate, maxdate=maxdate,
                        datetype=datetype, get=get, sleep=sleep)
        return handle.count, fetch_all(handle, max_articles=max_articles,
                                       get=get, sleep=sleep, progress=progress)
    import datetime
    today_year = datetime.date.today().year + 1
    if first_year is None and (mindate or maxdate):
        first_year = _parse(mindate).year if mindate else 1900
        last_year = _parse(maxdate).year if maxdate else today_year
    else:
        last_year = today_year
    handles = search_slices(term, first_year=first_year or 1900,
                            last_year=last_year, get=get, sleep=sleep,
                            mindate=mindate, maxdate=maxdate, datetype=datetype)
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
