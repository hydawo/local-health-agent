"""`literature refresh`: what PubMed has added to an installed pack's query
since the last refresh, plus retractions, folded into the corpus.

Two queries per pack. Additions use the Entrez date (the day PubMed added
the record), because "new since my last refresh" means indexed since then,
and a 2025 paper indexed in 2026 would be missed by publication date.
Retractions are asked for without a window or an evidence filter: a
retracted trial is retracted whatever its tier, and a snapshot would cite
it forever.

Orchestration only: the network is `eutils` and `client`, the writes are
`corpus.add` and `corpus.mark_retracted`. Nothing here prints.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .. import corpus
from ..packs import PACK_LICENSE, PackSpec
from . import client, eutils

RETRACTED_PT = '"Retracted Publication"[Publication Type]'
# One day of overlap at the start of every window: a record indexed on the
# boundary day is fetched twice rather than never, and the upsert makes the
# second fetch a no-op.
OVERLAP_DAYS = 1


class NotRefreshable(ValueError):
    """`sample` is a fixed snapshot; there is nothing to refresh it toward."""


@dataclass
class RefreshStats:
    window_from: str
    window_to: str
    matched: int
    added: int
    retracted: int
    chunks: int


def _date_part(stamp: str) -> date:
    return datetime.fromisoformat(stamp).date()


def window_for(built_at: str, refreshed_at: str | None, *, since: str | None,
               today: date) -> tuple[str, str]:
    if since:
        start = date.fromisoformat(since)
    else:
        start = _date_part(refreshed_at or built_at) - timedelta(days=OVERLAP_DAYS)
    return start.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")


def retraction_term(spec: PackSpec) -> str:
    return f"({spec.query}) AND {RETRACTED_PT}"


def refresh_pack(conn, spec: PackSpec, *, since: str | None = None,
                 today: date | None = None, get=client.get, sleep=time.sleep,
                 progress=None) -> RefreshStats:
    if spec.max_articles is not None:
        raise NotRefreshable(f"{spec.slug} is a fixed snapshot of "
                             f"{spec.max_articles} articles; install a topical "
                             f"pack to have something to refresh")
    row = conn.execute("SELECT built_at, refreshed_at FROM pack WHERE slug = ?",
                       (spec.slug,)).fetchone()
    if row is None:
        raise corpus.PackNotInstalled(f"{spec.slug} is not installed")
    window_from, window_to = window_for(row["built_at"], row["refreshed_at"],
                                        since=since, today=today or date.today())

    # Both fetches complete before anything is written, so a failure in
    # either leaves the corpus exactly as it was.
    matched, additions = eutils.fetch_term(
        spec.search_term(), sort=None, max_articles=None, first_year=None,
        mindate=window_from, maxdate=window_to, datetype="edat",
        get=get, sleep=sleep, progress=progress)
    _, retracted_articles = eutils.fetch_term(
        retraction_term(spec), sort=None, max_articles=None,
        first_year=spec.since_year, get=get, sleep=sleep)

    # `add()`'s upsert overwrites retracted/retraction_note from the freshly
    # parsed record, so it must run before mark_retracted: an addition that
    # happens to also be a retraction (no markup of its own, matched by the
    # separate retraction query) would otherwise clobber the mark. `add()`
    # is given retracted=0 here and the log row is corrected afterwards,
    # once the real count is known.
    # This is three separate commits (add, mark, correct); a crash between
    # them leaves that refresh's logged retracted count at 0 even though
    # the corpus itself is already right. The next refresh's retraction
    # query has no window, so it finds the same retraction again, but
    # `mark_retracted`'s `AND retracted = 0` means it is already marked and
    # is simply not counted again; only the historical log row for the
    # interrupted run stays wrong.
    stats = corpus.add(conn, additions, slug=spec.slug, license=PACK_LICENSE,
                       window_from=window_from, window_to=window_to,
                       matched=matched, retracted=0)
    notes = {a.pmid: a.retraction_note for a in retracted_articles}
    flipped = corpus.mark_retracted(conn, notes)
    conn.execute(
        "UPDATE refresh_log SET retracted = ? WHERE id = "
        "(SELECT id FROM refresh_log WHERE pack_id = "
        "(SELECT id FROM pack WHERE slug = ?) ORDER BY id DESC LIMIT 1)",
        (flipped, spec.slug))
    conn.commit()
    return RefreshStats(window_from, window_to, matched, stats.added, flipped,
                        stats.chunks)


__all__ = ["NotRefreshable", "RefreshStats", "RETRACTED_PT", "refresh_pack",
           "retraction_term", "window_for"]
