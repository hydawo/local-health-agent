# health_agent/store/workout_stats.py
"""The statistics and metadata a workout carries, read out of the JSON the
parser already stores on the row.

`ingest/healthkit.py` writes two blobs per workout: `metadata_json`, an object
of MetadataEntry key to value, and `statistics_json`, a list of
WorkoutStatistics attribute dicts. Both have been written since the first
ingest, so every number this module yields was already in the index and
nothing here reads an export.
"""
from __future__ import annotations

import json
import math

# The aggregates the export records on a WorkoutStatistics element. Order is
# the order rows come back in, so a reader sees average before maximum.
STAT_AGGS = ("average", "minimum", "maximum", "sum")
INDOOR_KEY = "HKIndoorWorkout"

# The key grammar, owned here because this module mints the keys. A statistic
# is `{type}{SEPARATOR}{aggregate}`; a metadata key is whatever Apple wrote and
# carries no aggregate. Three readers used to infer which was which by looking
# for a colon themselves, each with its own copy of the rule: the sample-depth
# base types, the `workouts --samples` guard, and the measure labels. They
# agreed on every real export and were still three rules for one fact.
SEPARATOR = ":"


def stat_key(type_name: str, aggregate: str) -> str:
    """The key a statistic is stored under."""
    return f"{type_name}{SEPARATOR}{aggregate}"


def is_stat_key(key: str) -> bool:
    """Whether a key names an aggregate, which only a statistic's does.

    For a key with no row in hand. Where the row is there, its stored `kind`
    is the answer and this is not asked.
    """
    return SEPARATOR in key


def base_type(key: str) -> str:
    """The record type a key is about, which is the key itself for metadata."""
    return key.split(SEPARATOR, 1)[0]


def aggregate_of(key: str) -> str:
    """The aggregate a statistic key names, or "" for a key that names none.

    Split on the first separator only: a type carrying a separator of its own
    would otherwise lose everything after the second, which is a silent
    truncation rather than a refusal.
    """
    return key.partition(SEPARATOR)[2]
_TRUTHY = {"1", "true", "yes"}
_FALSY = {"0", "false", "no"}


def _loads(blob: str | None, want: type):
    """Parse `blob`, or return None when it is absent, malformed, or not the
    shape this caller needs. Never raises."""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError, RecursionError):
        return None
    return parsed if isinstance(parsed, want) else None


def _num(value) -> float | None:
    """A finite number, or None. Booleans and NaN or infinity take the text
    branch rather than becoming numbers."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def stat_rows(statistics_json: str | None, metadata_json: str | None
              ) -> list[tuple[str, str, float | None, str | None, str | None]]:
    """`(kind, key, value_num, value_text, unit)` per entry, statistics first.

    A statistic becomes one row per aggregate the export recorded, keyed
    `{type}:{aggregate}`, so a heart-rate average and a heart-rate maximum are
    separate rows under names the export itself used. A value that is not a
    number is kept as text rather than dropped or coerced.
    """
    rows: list[tuple[str, str, float | None, str | None, str | None]] = []
    for entry in _loads(statistics_json, list) or []:
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type")
        if not isinstance(type_name, str) or not type_name:
            continue
        unit = entry.get("unit")
        for agg in STAT_AGGS:
            if agg not in entry:
                continue
            raw = entry[agg]
            value = _num(raw)
            rows.append(("statistic", stat_key(type_name, agg), value,
                         None if value is not None or raw is None else str(raw),
                         unit))
    for key, raw in sorted((_loads(metadata_json, dict) or {}).items()):
        value = _num(raw)
        rows.append(("metadata", key, value,
                     None if value is not None or raw is None else str(raw),
                     None))
    return rows


def indoor_flag(metadata_json: str | None) -> int | None:
    """1, 0, or None for `HKIndoorWorkout`. None means the export did not say,
    which is different from saying outdoor."""
    raw = (_loads(metadata_json, dict) or {}).get(INDOOR_KEY)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return 1
    if text in _FALSY:
        return 0
    return None
