"""The agent's tool contract.

Three tools, matching plan §4: `query_healthkit`, `search_records`,
`get_lab_trend`. Each is a thin adapter over `store/queries.py` — the same
functions the CLI calls — so the model and `health-agent metric` cannot report
different numbers for the same question. A tool that reimplemented its own
aggregation would be a second source of truth, and the two would drift.

**Context budget (plan §4).** A single question can touch all three tools, and
raw results are unbounded: a daily step series over three years is 1,100 points,
and a vector search can return whole pages of a PDF. Every tool here caps what it
returns *to the model*, separately from what it queried, and says so in the
payload when it truncates. The caps are deliberately small — a model reasoning
about a trend needs its shape and endpoints, not every sample — and the
`truncated` flag exists so the model can say "I looked at the last 60 days"
rather than silently answering from a slice it didn't know was a slice.

**Tools return data, never prose.** Each returns JSON with values and citations
attached. Phrasing, hedging, and the decision about what is worth saying belong
to the model; deciding what is *true* belongs here.

**Absence is data.** When a query finds nothing, the tools return the coverage
that does exist (`available_from`, `available_to`, `total_records`) instead of an
empty result. Without that, a model asked about a period it has no data for will
reach for the nearest numbers it can see and present them as an answer. This is
the mechanism behind the missing-data behavior in plan §5a.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import labs, metrics
from ..literature import tiers as lit_tiers
from ..logging_setup import get_logger
from ..store import queries, vector_store

log = get_logger("agent.tools")

# --- Context budget caps ---------------------------------------------------- #
# Points returned from a time series. 60 covers two months daily, a year weekly,
# or five years monthly — enough to describe a trend without flooding context.
MAX_SERIES_POINTS = 60
# Lab results per analyte. Someone with a decade of annual panels has ~10.
MAX_LAB_POINTS = 40
# Search snippets, and characters per snippet. Five chunks of ~600 chars is
# roughly 800 tokens, which leaves room for the other tools in one turn.
MAX_SEARCH_RESULTS = 8
MAX_SNIPPET_CHARS = 600
# Literature findings per call. Deliberately smaller than MAX_SEARCH_RESULTS:
# each finding carries a citation, a tier, and a year alongside its text, so
# five is already a substantial share of one turn's context.
MAX_LITERATURE_FINDINGS = 5
MAX_FINDING_CHARS = 700


@dataclass
class ToolContext:
    """Everything a handler needs, passed in rather than imported.

    `embedder_factory` is a callable rather than an embedder so that semantic
    search degrades to keyword search when Ollama has no embedding model, without
    the tool layer needing to know anything about embedding backends.
    """

    conn: Any
    vector_path: Any
    embedder_factory: Callable[[], Any] | None = None
    literature_conn: Any | None = None
    literature_vector_path: Any | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], dict]

    def schema(self) -> dict:
        """The tool definition in the shape Ollama's /api/chat expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- #
# query_healthkit
# --------------------------------------------------------------------------- #

def _query_healthkit(ctx: ToolContext, args: dict) -> dict:
    name = (args.get("metric") or "").strip()
    if not name:
        return {"error": "metric is required"}

    metric = metrics.resolve(name)
    if metric.identifier == "HKCategoryTypeIdentifierSleepAnalysis":
        return _query_sleep(ctx, args)
    period = (args.get("period") or "day").lower()
    if period not in queries.PERIODS:
        period = "day"
    aggregation = (args.get("aggregation") or "").lower() or None
    if aggregation and aggregation not in queries.AGGREGATIONS:
        aggregation = None

    try:
        result = queries.metric_series(
            ctx.conn, metric, period=period, agg=aggregation,
            start=args.get("start_date"), end=args.get("end_date"),
            source=args.get("source"),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    if result.is_empty:
        # Absence reported as coverage, not as an empty list. See module docstring.
        payload = {
            "metric": metric.identifier,
            "label": metric.label,
            "points": [],
            "no_data_in_range": True,
            "total_records_for_metric": result.total_records,
            "available_from": result.available_from,
            "available_to": result.available_to,
        }
        if not result.total_records:
            payload["note"] = (
                f"There is no {metric.label} data in this index at all. Do not "
                f"substitute a related metric; say it is missing and ask whether "
                f"the user tracks it."
            )
        else:
            payload["note"] = (
                f"{metric.label} exists but not in the requested window. Say so "
                f"and give the range that is available; do not answer with data "
                f"from a different period as though it covered this one."
            )
        return payload

    points = result.points
    truncated = False
    if len(points) > MAX_SERIES_POINTS:
        points = points[-MAX_SERIES_POINTS:]
        truncated = True

    payload: dict = {
        "metric": metric.identifier,
        "label": metric.label,
        "aggregation": result.agg,
        "period": result.period,
        "unit": result.unit,
        # The aggregate over the whole requested range, computed here so the
        # model never has to. Measured before this existed: asked for an average
        # resting heart rate, the model summed seven daily values by hand and
        # returned 59.6 where the answer is exactly 60.0. Language models are
        # unreliable arithmetic engines, and a wrong number delivered fluently is
        # this project's worst failure mode — so any figure the model might
        # otherwise compute is precomputed and handed to it.
        "overall": _overall(result.points, result.agg),
        "points": [
            {"period": p.period, "value": p.value, "samples": p.n,
             **({"category": p.category} if p.category else {})}
            for p in points
        ],
        "available_from": result.available_from,
        "available_to": result.available_to,
    }
    if truncated:
        payload["truncated"] = True
        payload["truncation_note"] = (
            f"Showing the most recent {MAX_SERIES_POINTS} of "
            f"{len(result.points)} periods. Say that the answer covers this "
            f"window rather than the whole history."
        )
    if result.multi_source_periods and not args.get("source"):
        payload["multi_source_warning"] = (
            f"{result.multi_source_periods} period(s) had more than one recording "
            f"device ({', '.join(result.sources_seen)}). Values shown take the "
            f"largest single source rather than adding devices together, because "
            f"summing overlapping devices double-counts. Mention this if the "
            f"total matters."
        )
    if len(result.units_seen) > 1:
        payload["unit_warning"] = (
            f"Mixed units in this range: {', '.join(result.units_seen)}. Values "
            f"are not converted."
        )
    return payload


# --------------------------------------------------------------------------- #
# get_lab_trend
# --------------------------------------------------------------------------- #

def _query_sleep(ctx: ToolContext, args: dict) -> dict:
    """Sleep, attributed to nights rather than calendar dates.

    A night's sleep spans midnight: the in-bed segment lands on one date and the
    deep and REM stages on the next. Answering "how much deep sleep on the night
    of the 4th" from calendar dates returns nothing for the 4th — which the
    model then reports as an absence of deep-sleep data. Grouping by night
    (noon boundary) is what the question actually means.
    """
    start, end = args.get("start_date"), args.get("end_date")
    nights = queries.sleep_nights(ctx.conn, start=start, end=end)

    if not nights:
        total, first, last = queries.sleep_coverage(ctx.conn)
        return {
            "metric": "sleep",
            "nights": [],
            "no_data_in_range": True,
            "total_sleep_records": total,
            "available_from": first,
            "available_to": last,
            "note": ("No sleep in that range. Nights are labelled by the "
                     "evening they began, so the night of the 4th includes "
                     "segments recorded after midnight on the 5th."),
        }

    trimmed = nights[-MAX_SERIES_POINTS:]
    return {
        "metric": "sleep",
        "attribution": ("Each night is labelled by the evening it began; "
                        "segments after midnight count toward the night "
                        "before."),
        "nights": [
            {
                "night_of": n["night"],
                "asleep_hours": round(n["asleep_seconds"] / 3600, 2),
                "in_bed_hours": round(n["in_bed_seconds"] / 3600, 2),
                "stages_hours": {stage: round(seconds / 3600, 2)
                                 for stage, seconds in sorted(n["stages"].items())},
                "sources": n["sources"],
            }
            for n in trimmed
        ],
        "truncated": len(nights) > len(trimmed) or None,
    }


def _get_workouts(ctx: ToolContext, args: dict) -> dict:
    start, end = args.get("start_date"), args.get("end_date")
    limit = min(int(args.get("limit") or 25), 50)
    sessions = queries.list_workouts(ctx.conn, start=start, end=end, limit=limit)

    if not sessions:
        total, first, last = queries.workout_coverage(ctx.conn)
        return {
            "workouts": [],
            "no_data_in_range": True,
            "total_workouts": total,
            "available_from": first,
            "available_to": last,
            "note": ("No workouts in that range. Say so rather than inferring "
                     "activity from step counts or energy burned."),
        }

    totals = queries.workout_summary(ctx.conn, start=start, end=end)
    return {
        "workouts": [
            {
                "date": w["local_date"],
                "activity": (w["activity_type"] or "")
                .replace("HKWorkoutActivityType", ""),
                "duration": w["duration"], "duration_unit": w["duration_unit"],
                "distance": w["total_distance"],
                "distance_unit": w["total_distance_unit"],
                "energy": w["total_energy"], "energy_unit": w["total_energy_unit"],
                "source": w["source_name"],
            }
            for w in sessions
        ],
        "totals_by_activity": [
            {
                "activity": (t["activity_type"] or "")
                .replace("HKWorkoutActivityType", ""),
                "count": t["n"], "total_duration": t["total_duration"],
                "duration_unit": t["duration_unit"],
                "total_distance": t["total_distance"],
                "distance_unit": t["total_distance_unit"],
                "first": t["first"], "last": t["last"],
            }
            for t in totals
        ],
    }


def _overall(points: list, aggregation: str) -> dict | None:
    """Collapse a series to one figure covering the whole requested range.

    Each point's value is already combined across sources for its period, so the
    remaining question is only how to combine periods. Averages are weighted by
    sample count: a day with 300 heart-rate samples should not count the same as
    a day with 3.
    """
    values = [(p.value, p.n) for p in points if p.value is not None]
    if not values:
        return None

    total_samples = sum(n for _, n in values)
    if aggregation in ("sum", "duration", "count"):
        figure = sum(v for v, _ in values)
    elif aggregation == "min":
        figure = min(v for v, _ in values)
    elif aggregation == "max":
        figure = max(v for v, _ in values)
    elif aggregation == "avg":
        figure = (sum(v * n for v, n in values) / total_samples
                  if total_samples else None)
    else:
        return None

    return {
        "value": figure,
        "aggregation": aggregation,
        "periods_covered": len(values),
        "total_samples": total_samples,
        "from": points[0].period_start,
        "to": points[-1].period_end,
        "note": ("Use this value directly. Do not recompute it from the points "
                 "below."),
    }


# Analytes per batched call, and points kept for each. Batching exists because a
# broad question ("anything out of range?") otherwise becomes one call per
# analyte: measured at 23 calls and 358 seconds before this parameter existed.
# Points are trimmed harder in batch mode since the question is usually about
# the latest value and its direction, not the full history.
MAX_BATCH_ANALYTES = 30
MAX_BATCH_POINTS = 6


def _get_lab_trend(ctx: ToolContext, args: dict) -> dict:
    batch = args.get("analytes")
    if isinstance(batch, str):
        batch = [a.strip() for a in batch.split(",") if a.strip()]
    if batch:
        return _get_lab_trends_batch(ctx, batch, args)

    name = (args.get("analyte") or "").strip()
    if not name:
        return {"error": "analyte or analytes is required"}

    key, _ = labs.resolve_key(name)
    trend = queries.lab_trend(ctx.conn, key, start=args.get("start_date"),
                              end=args.get("end_date"))

    if trend.is_empty:
        available = queries.list_analytes(ctx.conn, limit=60)
        payload = {
            "analyte": key,
            "label": trend.label,
            "points": [],
            "no_data": True,
            "total_results_for_analyte": trend.total_results,
            "available_from": trend.available_from,
            "available_to": trend.available_to,
            "analytes_present_in_reports": [a["analyte_key"] for a in available],
            "note": (
                f"No {trend.label} results in the requested range. Say so "
                f"explicitly. Do not infer a value from a different analyte."
            ),
        }
        return payload

    points = trend.points
    truncated = False
    if len(points) > MAX_LAB_POINTS:
        points = points[-MAX_LAB_POINTS:]
        truncated = True

    payload: dict = {
        "analyte": trend.key,
        "label": trend.label,
        "points": [
            {
                "collected_date": p.collected_date,
                "value": p.value_num if p.value_num is not None else p.value_text,
                "unit": p.unit,
                "reference_range": p.ref_text,
                "flag": p.flag,
                "out_of_range": p.out_of_range,
                "citation": p.citation,
                **({"via_ocr": True} if p.via_ocr else {}),
            }
            for p in points
        ],
    }
    if truncated:
        payload["truncated"] = True
    if len(trend.units_seen) > 1:
        payload["unit_warning"] = (
            f"Results use different units across reports "
            f"({', '.join(trend.units_seen)}) and are NOT converted. Do not "
            f"compare them numerically without saying this."
        )
    if any(p.via_ocr for p in points):
        payload["ocr_warning"] = (
            "Values marked via_ocr were read from a scanned page. OCR misreads "
            "digits, so reference ranges especially may be wrong. Say that these "
            "should be checked against the original report."
        )
    return payload


# --------------------------------------------------------------------------- #
# search_records
# --------------------------------------------------------------------------- #

def _get_lab_trends_batch(ctx: ToolContext, names: list, args: dict) -> dict:
    """Several analytes in one call, trimmed to the most recent results each."""
    requested = [str(n).strip() for n in names if str(n).strip()]
    truncated_list = len(requested) > MAX_BATCH_ANALYTES
    requested = requested[:MAX_BATCH_ANALYTES]

    found: dict[str, dict] = {}
    missing: list[str] = []
    flagged: list[dict] = []

    for name in requested:
        key, _ = labs.resolve_key(name)
        trend = queries.lab_trend(ctx.conn, key, start=args.get("start_date"),
                                  end=args.get("end_date"))
        if trend.is_empty:
            missing.append(key)
            continue
        points = trend.points[-MAX_BATCH_POINTS:]
        found[key] = {
            "label": trend.label,
            "results": [
                {
                    "collected_date": p.collected_date,
                    "value": p.value_num if p.value_num is not None else p.value_text,
                    "unit": p.unit,
                    "reference_range": p.ref_text,
                    "flag": p.flag,
                    "out_of_range": p.out_of_range,
                    "citation": p.citation,
                    **({"via_ocr": True} if p.via_ocr else {}),
                }
                for p in points
            ],
        }
        latest = trend.points[-1]
        if latest.out_of_range:
            flagged.append({
                "analyte": key, "label": trend.label,
                "value": latest.value_num, "unit": latest.unit,
                "reference_range": latest.ref_text, "flag": latest.flag,
                "collected_date": latest.collected_date,
                "citation": latest.citation,
            })

    payload: dict = {
        "analytes": found,
        # Precomputed so the model doesn't have to scan every result itself and
        # risk missing one — the same principle as `overall` for time series.
        "out_of_range_on_latest_report": flagged,
    }
    if missing:
        payload["no_results_for"] = missing
    if truncated_list:
        payload["truncated_request"] = (
            f"Only the first {MAX_BATCH_ANALYTES} analytes were looked up.")
    if any(r.get("via_ocr") for a in found.values() for r in a["results"]):
        payload["ocr_warning"] = (
            "Values marked via_ocr were read from a scanned page. OCR misreads "
            "digits, so reference ranges especially may be wrong. Say that these "
            "should be checked against the original report.")
    return payload


def _search_records(ctx: ToolContext, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    limit = min(int(args.get("limit") or 5), MAX_SEARCH_RESULTS)
    kind = args.get("kind")
    if kind not in ("note", "pdf", "image", None):
        kind = None

    hits: list = []
    method = "keyword"
    if ctx.embedder_factory is not None:
        try:
            embedder = ctx.embedder_factory()
            embedded = ctx.conn.execute(
                "SELECT COUNT(*) AS n FROM chunk WHERE embedded_with = ?",
                (embedder.name,),
            ).fetchone()["n"]
            if embedded:
                store = vector_store.VectorStore(ctx.vector_path)
                hits = vector_store.semantic_search(
                    ctx.conn, store, embedder, query, limit=limit, kind=kind)
                method = "semantic"
        except Exception as exc:  # noqa: BLE001 - retrieval must not kill a turn
            log.warning("semantic search unavailable (%s)", type(exc).__name__)

    if not hits:
        hits = vector_store.keyword_search(ctx.conn, query, limit=limit, kind=kind)
        method = "keyword"

    if not hits:
        total = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
        tags = [r["tag"] for r in ctx.conn.execute(
            "SELECT DISTINCT tag FROM document_tag ORDER BY tag")]
        return {
            "results": [],
            "no_matches": True,
            "chunks_indexed": total,
            "tags_available": tags,
            "note": ("Nothing matched. Say that the documents and notes do not "
                     "cover this, rather than answering from general knowledge."),
        }

    return {
        "method": method,
        "results": [
            {
                "citation": h.citation,
                "kind": h.kind,
                "text": " ".join(h.text.split())[:MAX_SNIPPET_CHARS],
            }
            for h in hits
        ],
        "note": ("Quote or paraphrase only what these snippets say, and cite the "
                 "citation string with any claim drawn from them."),
    }


# --------------------------------------------------------------------------- #
# search_medical_literature
# --------------------------------------------------------------------------- #

def _search_medical_literature(ctx: ToolContext, args: dict) -> dict:
    from ..literature import corpus as lit_corpus

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    if ctx.literature_conn is None:
        # Not an error: a corpus is optional. But the model must be told that
        # the absence is why there is nothing here, so it does not fall back on
        # recalled medical knowledge — the failure this tool exists to fix.
        return {
            "corpus_installed": False,
            "findings": [],
            "note": ("A literature corpus is not installed on this machine. Say "
                     "that you have no literature to cite and that you cannot "
                     "state clinical thresholds or general medical facts "
                     "without one. Do not answer from general knowledge."),
        }

    limit = min(int(args.get("limit") or MAX_LITERATURE_FINDINGS),
                MAX_LITERATURE_FINDINGS)
    min_tier = args.get("min_tier") or None
    since_year = args.get("since_year") or None

    try:
        findings = _literature_hits(ctx, query, limit, min_tier, since_year)
    except ValueError as exc:
        return {"error": str(exc)}

    coverage = lit_corpus.coverage(ctx.literature_conn)

    # A floor excludes every unranked article, and on a real corpus that is
    # most of it: 65.5% of 2,000 PubMed abstracts resolved to `unknown`
    # because PubMed records no study design for most primary research. The
    # numbers here are corpus-level and exact, from coverage(); a per-query
    # count over an FTS OR-match would be true and useless. Protocols are
    # counted separately (payload["protocol_warning"], below): a protocol has
    # a known design, it simply reports no results, so it is not part of
    # "no recorded study design".
    filter_block: dict | None = None
    if min_tier or since_year:
        filter_block = {"min_tier": min_tier, "since_year": since_year}
        if min_tier:
            unranked = coverage["tiers"].get(lit_tiers.UNKNOWN, 0)
            total = coverage["article_count"] or 1
            filter_block["unranked_articles_excluded"] = unranked
            filter_block["unranked_share"] = round(unranked / total, 3)

    if not findings:
        miss: dict = {
            "corpus_installed": True,
            "findings": [],
            "no_matches": True,
            "corpus": coverage,
        }
        if filter_block is None:
            miss["note"] = (
                "The corpus holds nothing on this. Say that your literature "
                "does not cover it, name what it does cover, and do not answer "
                "from general knowledge.")
        else:
            # Not a coverage gap: the corpus may hold plenty on this, untagged.
            # Saying 'holds nothing' here would send the model to recall.
            miss["filtered"] = True
            miss["filter"] = filter_block
            reason = "Nothing matched under these filters. "
            if min_tier:
                reason += (
                    f"{filter_block['unranked_articles_excluded']} of "
                    f"{coverage['article_count']} articles in this corpus "
                    f"({filter_block['unranked_share']:.0%}) have no recorded "
                    f"study design and are excluded by any min_tier floor; "
                    f"that is normal for PubMed, not a sign of thin coverage. ")
            miss["note"] = (
                reason + "Retry without min_tier and since_year before "
                "concluding that the corpus does not cover this, and do not "
                "answer from general knowledge.")
        return miss

    payload: dict = {
        "corpus_installed": True,
        "corpus": {"packs": coverage["packs"], "built": coverage["built"],
                   "article_count": coverage["article_count"]},
        "method": findings[0].method,
        "findings": [
            {
                "title": f.title,
                "text": " ".join(f.text.split())[:MAX_FINDING_CHARS],
                "citation": f.citation,
                "pmid": f.pmid,
                "doi": f.doi,
                "year": f.year,
                "evidence_tier": f.evidence_tier,
                "tier_source": f.tier_source,
                "license": f.license,
                "retracted": f.retracted,
                **({"retraction_note": f.retraction_note} if f.retracted else {}),
            }
            for f in findings
        ],
        "note": (
            "These are findings about populations from published research, not "
            "facts about this person. State each one separately with its "
            "evidence tier and its year — 'a 2019 meta-analysis found...' — and "
            "cite it with its PMID, so the reader can check it at the source. "
            "Do not merge several findings into a single conclusion, "
            "and do not turn any of them into a recommendation about what this "
            "person should do."
        ),
    }
    if any(f.retracted for f in findings):
        payload["retraction_warning"] = (
            "One or more findings come from a RETRACTED publication. Say so "
            "beside the citation, and do not present it as current evidence.")
    if any(f.evidence_tier == lit_tiers.UNKNOWN for f in findings):
        payload["tier_warning"] = (
            "Findings with evidence_tier 'unknown' had no publication type "
            "recorded. Say that their study design is unknown rather than "
            "implying one.")
    if filter_block is not None:
        payload["filter"] = filter_block
        if min_tier:
            payload["filter_note"] = (
                f"min_tier={min_tier} excluded the "
                f"{filter_block['unranked_articles_excluded']} articles "
                f"({filter_block['unranked_share']:.0%} of this corpus) that "
                f"have no recorded study design. Without a floor, results "
                f"are ordered strongest-first among the matches, but the "
                f"matches themselves are chosen by relevance, so a floor "
                f"can surface strong evidence a broad query would not.")
    if any(f.evidence_tier == lit_tiers.PROTOCOL for f in findings):
        payload["protocol_warning"] = (
            "One or more findings are trial PROTOCOLS: they describe a "
            "planned study and report no results. Say so beside the citation, "
            "and do not present a protocol as evidence of anything.")
    return payload


def _literature_hits(ctx: ToolContext, query: str, limit: int,
                     min_tier: str | None, since_year: int | None) -> list:
    """Semantic where possible, keyword otherwise. Never raises for retrieval."""
    from ..literature import embed as lit_embed
    from ..literature import store as lit_store
    from ..store import vector_store

    if ctx.embedder_factory is not None and ctx.literature_vector_path:
        try:
            embedder = ctx.embedder_factory()
            store = vector_store.VectorStore(ctx.literature_vector_path,
                                             table_name=lit_embed.TABLE_NAME)
            return lit_store.search(ctx.literature_conn, store, embedder, query,
                                    limit=limit, min_tier=min_tier,
                                    since_year=since_year)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - retrieval must not kill a turn
            log.warning("corpus semantic search unavailable (%s)",
                        type(exc).__name__)
    return lit_store.keyword_search(ctx.literature_conn, query, limit=limit,
                                    min_tier=min_tier, since_year=since_year)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="query_healthkit",
        description=(
            "Aggregate a metric from the user's Apple Health export over time "
            "(heart rate, steps, sleep, HRV, weight, and similar). Use for "
            "anything a watch or phone recorded. Returns one value per day, "
            "week, or month with the units."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "Metric name, e.g. resting-hr, steps, sleep, hrv, "
                        "weight, active-energy, respiratory-rate. Accepts full "
                        "HealthKit identifiers too. Asking for 'sleep' returns "
                        "per-night totals with stages broken out, where a night "
                        "includes the hours after midnight."
                    ),
                },
                "start_date": {"type": "string",
                               "description": "Inclusive, YYYY-MM-DD."},
                "end_date": {"type": "string",
                             "description": "Inclusive, YYYY-MM-DD."},
                "period": {
                    "type": "string",
                    "enum": ["day", "week", "month"],
                    "description": "Bucket size. Defaults to day.",
                },
                "aggregation": {
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count", "duration"],
                    "description": (
                        "Omit this unless the user asked for something specific. "
                        "The correct default is chosen per metric: cumulative "
                        "metrics like steps are summed, point-in-time metrics "
                        "like heart rate are averaged."
                    ),
                },
            },
            "required": ["metric"],
        },
        handler=_query_healthkit,
    ),
    Tool(
        name="get_lab_trend",
        description=(
            "Blood-test results from the user's lab reports, oldest first, with "
            "the reference range and flag each report printed and a citation to "
            "the source file and page. Use for anything from bloodwork. "
            "For a broad question such as which results are abnormal, pass "
            "several names in `analytes` in ONE call rather than calling this "
            "repeatedly — the reply then includes a ready-made list of every "
            "value flagged out of range on the latest report."
        ),
        parameters={
            "type": "object",
            "properties": {
                "analyte": {
                    "type": "string",
                    "description": (
                        "A single analyte, e.g. ldl, hdl, hba1c, tsh, glucose, "
                        "vitamin_d, ferritin, creatinine. Returns its full "
                        "history."
                    ),
                },
                "analytes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Several analytes at once. Use this for broad questions. "
                        "Returns the most recent few results for each plus "
                        "`out_of_range_on_latest_report`."
                    ),
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
            },
        },
        handler=_get_lab_trend,
    ),
    # A fourth tool, beyond the three named in plan §4, added because the eval
    # set caught its absence: asked "what workouts have I logged?", the agent
    # answered "none" while the index held a logged run. Workouts live in their
    # own table with their own shape (activity, duration, distance, energy), and
    # nothing in the other three tools could see them. §4 allows for 4-5 tools.
    Tool(
        name="get_workouts",
        description=(
            "Workout sessions the user recorded — activity type, date, "
            "duration, distance, and energy. Use for any question about "
            "workouts, exercise sessions, runs, rides, or training. Step counts "
            "and active energy are NOT workouts; use query_healthkit for those."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
                "limit": {"type": "integer",
                          "description": "Max sessions to return, up to 50."},
            },
        },
        handler=_get_workouts,
    ),
    Tool(
        name="search_records",
        description=(
            "Search the text of the user's medical records and personal notes. "
            "Use for context, symptoms, what a report said in prose, or anything "
            "the user wrote down. Returns snippets with citations. Not for "
            "numeric lab values — use get_lab_trend for those."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to look for, in natural language."},
                "kind": {
                    "type": "string",
                    "enum": ["note", "pdf", "image"],
                    "description": (
                        "Restrict to personal notes, to medical records (PDFs), "
                        "or to photos and screenshots read by OCR. Omit to "
                        "search all three."
                    ),
                },
                "limit": {"type": "integer",
                          "description": f"Max snippets, up to {MAX_SEARCH_RESULTS}."},
            },
            "required": ["query"],
        },
        handler=_search_records,
    ),
    Tool(
        name="search_medical_literature",
        description=(
            "Search a local corpus of published medical research for what the "
            "evidence says about a topic. Use this for ANY general medical "
            "claim — what a marker is associated with, what a threshold is, "
            "what research has found — because you must not state such things "
            "from your own knowledge. Returns individual findings, each with a "
            "dated citation and an evidence tier. These describe populations, "
            "NOT this person; use the other tools for their own data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "The topic, in natural language."},
                "min_tier": {
                    "type": "string",
                    "enum": list(lit_tiers.TIERS),
                    "description": (
                        "Floor on study design, strongest to weakest. Most "
                        "primary research in PubMed carries no study-design "
                        "tag, so ANY floor excludes the majority of the "
                        "corpus, not just weak studies. Without it, results "
                        "are ordered strongest-first among the relevance "
                        "matches. Set it only when "
                        "the user asks for evidence at a stated strength, and "
                        "if it returns nothing, retry without it."
                    ),
                },
                "since_year": {"type": "integer",
                               "description": "Only findings published since."},
                "limit": {"type": "integer",
                          "description": f"Max findings, up to {MAX_LITERATURE_FINDINGS}."},
            },
            "required": ["query"],
        },
        handler=_search_medical_literature,
    ),
)

BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS]


def dispatch(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Run one tool call. Errors come back as data, never as exceptions.

    A tool that raises would end the conversation; a tool that returns an error
    lets the model correct itself — usually by fixing an argument — which is the
    difference between a failed answer and a slower one.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}",
                "available_tools": list(BY_NAME)}
    if not isinstance(arguments, dict):
        return {"error": "arguments must be an object"}
    try:
        return tool.handler(ctx, arguments)
    except Exception as exc:  # noqa: BLE001
        log.warning("tool %s failed (%s)", name, type(exc).__name__)
        return {"error": f"{type(exc).__name__} while running {name}"}


def serialize(payload: dict) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Data inventory for the system prompt
# --------------------------------------------------------------------------- #

def data_inventory(conn, *, max_metrics: int = 25,
                   max_analytes: int = 30) -> dict:
    """A compact description of what this index actually contains.

    Injected into the system prompt rather than exposed as a fourth tool. A model
    that doesn't know which metrics exist will guess names, burn a turn on a
    failed lookup, and sometimes answer about a metric the user doesn't track.
    Telling it up front costs a few hundred tokens once and removes that whole
    failure class.
    """
    summary = queries.index_summary(conn)
    notes = queries.note_summary(conn)
    docs = queries.document_summary(conn)

    metric_names = []
    for row in queries.list_types(conn, limit=max_metrics):
        metric = metrics.lookup(row["type"])
        metric_names.append(metric.aliases[0] if metric and metric.aliases
                            else row["type"])

    analytes = [a["analyte_key"] for a in
                queries.list_analytes(conn, limit=max_analytes)]
    tags = [t["tag"] for t in queries.list_tags(conn, limit=25)]

    return {
        "healthkit": {
            "records": summary["records"],
            "date_range": [summary["first_date"], summary["last_date"]],
            "metrics_available": metric_names,
        },
        "labs": {
            "reports": docs["documents"],
            "date_range": [docs["doc_first"], docs["doc_last"]],
            "analytes_available": analytes,
        },
        "notes": {
            "count": notes["notes"],
            # Photos are notes, not reports: the model learns here that
            # kind="image" has something to return, and labs.reports never
            # counts them.
            "images": notes["images"],
            "date_range": [notes["first"], notes["last"]],
            "tags": tags,
        },
    }
