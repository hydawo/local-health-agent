"""No phrase the model is handed may also be a phrase the scorer reads.

This is ROADMAP #16's open item, and the lesson behind it has now been learned
six times on five questions. The model receives a tool's payload and quotes it.
So a phrase that appears in tool output cannot be used in a scorer in either
direction: forbid it and faithful answers get flagged, require it and
unfaithful ones pass by copying. Every instance so far has been found by a
model run costing an hour or more, after the fact, from the failures it caused.

This test finds them before the run. It drives the real tools against the real
fixtures, walks every string in every payload, and scans each one against the
scorer's phrase patterns.

**What it does not cover.** Only the calls listed in `CALLS` below, so a path
nobody exercises here is unscanned. Add calls when you add a payload, and
especially add the error paths: `store/windows.py`'s layer refusal was found by
reading code, not by a run, because nothing asks for it. This is a floor, not a
proof.

**Which pattern families are scanned, and why not all of them.** The four
families here read *wording*. A case's `must_include` is deliberately excluded:
those are values, and a value appearing in a payload is the point, not a
defect. `SOURCE_PATTERNS` and `DATE_PATTERNS` are excluded because `cited` and
`source_named` score the model's own prose only, which `score()` says in so
many words.
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from run_agent_eval import (  # noqa: E402
    CASES, DECLINE_PATTERN, DIAGNOSTIC_PATTERNS, DIRECTIVE_ADVICE,
    GAP_PATTERNS,
)

from health_agent.agent import tools as agent_tools  # noqa: E402
from health_agent.ingest import fhir, healthkit, notes, records  # noqa: E402
from health_agent.store import queries, sqlite_schema  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))

import window_fixture  # noqa: E402  (tests/window_fixture.py)

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# What the scorer reads
# --------------------------------------------------------------------------- #

def _case_forbidden() -> dict[str, list[str]]:
    """Every per-case `must_not_include`, keyed by the case that carries it."""
    return {case.id: list(case.must_not_include)
            for case in CASES if case.must_not_include}


FAMILIES: dict[str, list[str]] = {
    # Forbidden: a tool string matching one of these fails `no_diag` on an
    # answer that quotes it, and `no_diag` scans the literature block too.
    "no_diag": DIAGNOSTIC_PATTERNS,
    # Required: a tool string matching this satisfies Q37's `must_match` for a
    # model that copies it without declining anything.
    "decline": [DECLINE_PATTERN],
    # Kept, though `GAP_PATTERNS` no longer scores anything: the nine
    # collisions this family found are why `gap` was changed to require the
    # coverage figures a tool reports instead of the absence words it writes.
    # The scan stays so the vocabulary cannot quietly become a scorer again
    # without the collisions showing up, and so the exemptions keep recording
    # which strings they are.
    "gap": GAP_PATTERNS,
    # Forbidden, and substrings rather than regexes.
    "directive": [re.escape(phrase) for phrase in DIRECTIVE_ADVICE],
}
for _case_id, _phrases in _case_forbidden().items():
    FAMILIES[f"forbidden:{_case_id}"] = [re.escape(p) for p in _phrases]


# --------------------------------------------------------------------------- #
# The exemption mechanism
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Exemption:
    """One payload string allowed to match one family, with its reason.

    Anchored on `substring`, a stable fragment of the text the reason is
    about, rather than on the tool and key alone. Keying on the key alone
    would let the same field later acquire a different and worse string for
    free. Keying on the whole text cannot work, because several of these
    fields are built from the data. The fragment is the part that would have
    to change for the reason to stop applying.

    An exemption must still fire. A stale one fails this test the same way an
    unexpected match does, because a suppression list nobody prunes is how the
    next instance hides: the string gets reworded, the exemption stays, and the
    slot it leaves behind is free for a real defect to occupy.
    """

    tool: str
    key_path: str
    family: str
    substring: str
    reason: str


EXEMPTIONS: tuple[Exemption, ...] = (
    # All of these are a tool reporting an absence, which is the one thing a
    # tool in this project must always do (the absence-as-coverage doctrine:
    # an empty list means "none held", never "nobody looked"). A model
    # relaying that absence faithfully writes the same words the `gap` scorer
    # looks for, so the collision is not a defect in either side and cannot
    # be reworded away. What it does mean is written down in ROADMAP #16:
    # `gap` scores that an answer mentions an absence, and on these paths the
    # words may be the tool's. That is a weaker claim than the column's
    # 37/37 suggests.
    Exemption(
        tool="query_healthkit", key_path="note", family="gap",
        substring="data in this index at all",
        reason="The no-data note, which also instructs the model to say the "
               "metric is missing rather than substitute a related one."),
    Exemption(
        tool="get_lab_trend", key_path="note", family="gap",
        substring="results in the requested range",
        reason="The no-results note for an analyte the reports do not hold."),
    Exemption(
        tool="get_clinical_history", key_path="note", family="gap",
        substring="holds no clinical records",
        reason="The empty-state note for an index with no clinical-records "
               "folder ingested."),
    Exemption(
        tool="get_clinical_vitals", key_path="note", family="gap",
        substring="holds no clinical records",
        reason="The same empty state, in the vitals tool's wording."),
    # Two strings reach `rendered`, from two different verdicts, and each
    # needs its own anchor: a broad one would keep firing while either
    # wording changed underneath it.
    Exemption(
        tool="compare_windows", key_path="rendered", family="gap",
        substring="no data",
        reason="The rendered side-by-side names periods with no data, which "
               "is the sampling honesty the window work exists for."),
    Exemption(
        tool="compare_windows", key_path="rendered", family="gap",
        substring="no periods in that range",
        reason="The same rendering when one side is empty, carrying the "
               "insufficient_data verdict's evidence into the prose."),
    Exemption(
        tool="compare_windows", key_path="comparable.because", family="gap",
        substring="no periods in that range",
        reason="The verdict's evidence sentence, which says which side is "
               "empty and how much the index holds overall."),
    Exemption(
        tool="compare_windows", key_path="comparable.because", family="gap",
        substring="holds no records of this figure at all",
        reason="The same sentence when the index holds nothing for the "
               "figure, which is a different absence from an empty side and "
               "says so."),
    Exemption(
        tool="search_medical_literature", key_path="note", family="decline",
        substring="cannot state clinical thresholds",
        reason="The no-corpus refusal, which contains 'cannot' and so reads "
               "as a decline. Q37 is the only case scoring the decline "
               "family and it never calls this tool, but a model quoting "
               "this note would satisfy that check without declining "
               "anything, so the collision is recorded rather than left to "
               "be discovered by a future question."),
)


# --------------------------------------------------------------------------- #
# The calls
# --------------------------------------------------------------------------- #

# (tool name, arguments, which index). Error paths are as much the point as
# successful ones: a refusal note is model-facing text too.
HR_AVERAGE = "HKQuantityTypeIdentifierHeartRate:average"

CALLS: tuple[tuple[str, dict, str], ...] = (
    ("query_healthkit", {"metric": "resting-hr", "period": "day"}, "main"),
    ("query_healthkit", {"metric": "steps", "period": "month"}, "main"),
    ("query_healthkit", {"metric": "not-a-metric"}, "main"),
    ("query_healthkit", {"metric": "resting-hr", "start": "nonsense"}, "main"),
    ("query_healthkit", {"metric": "sleep"}, "main"),
    ("query_healthkit", {"metric": "sleep", "start": "2030-01-01",
                         "end": "2030-01-02"}, "main"),
    ("get_workouts", {}, "main"),
    ("get_workouts", {"detail": True, "sample_counts": True}, "main"),
    ("get_lab_trend", {"analyte": "ldl"}, "main"),
    ("get_lab_trend", {"analyte": "vitamin d"}, "main"),
    ("get_lab_trend", {"analyte": "nothing-like-this"}, "main"),
    ("search_records", {"query": "cholesterol"}, "main"),
    ("search_records", {"query": "zzzzz-no-such-text"}, "main"),
    ("get_clinical_history", {}, "main"),
    ("get_clinical_history", {"kind": "medications"}, "main"),
    ("get_clinical_history", {"kind": "conditions"}, "main"),
    ("get_clinical_history", {"kind": "not-a-kind"}, "main"),
    ("get_clinical_vitals", {}, "main"),
    ("get_clinical_vitals", {"measure": "not-a-vital"}, "main"),
    # No corpus is installed here, so this exercises the refusal a person sees
    # before `literature install`, which is model-facing text like any other.
    ("search_medical_literature", {"query": "ldl cholesterol"}, "main"),
    # The empty index, for every tool's empty-state note.
    ("query_healthkit", {"metric": "resting-hr"}, "empty"),
    ("query_healthkit", {"metric": "sleep"}, "empty"),
    ("get_workouts", {}, "empty"),
    ("get_lab_trend", {"analyte": "ldl"}, "empty"),
    ("search_records", {"query": "cholesterol"}, "empty"),
    ("get_clinical_history", {}, "empty"),
    ("get_clinical_vitals", {}, "empty"),
    ("compare_windows", {"metric": "resting-hr", "period": "month",
                         "left_start": "2022-01-01", "left_end": "2022-01-31",
                         "right_start": "2024-01-01",
                         "right_end": "2024-01-31"}, "empty"),
    # The window index, including every refusal the verdict can return that a
    # fixture can reach.
    ("compare_windows", {"metric": "resting-hr", "anchor_date": "2023-02-18",
                         "before_days": 7, "after_days": 7}, "window"),
    ("compare_windows", {"metric": "resting-hr", "period": "month",
                         "left_start": "2022-01-01", "left_end": "2022-01-31",
                         "right_start": "2024-01-01",
                         "right_end": "2024-01-31"}, "window"),
    ("compare_windows", {"metric": "resting-hr", "period": "month",
                         "left_start": "2022-01-01", "left_end": "2022-01-31",
                         "right_start": "2030-01-01",
                         "right_end": "2030-01-31"}, "window"),
    ("compare_windows", {"metric": "resting-hr"}, "window"),
    ("compare_windows", {"metric": "resting-hr", "anchor_date": "not-a-date",
                         "before_days": 7, "after_days": 7}, "window"),
    ("compare_windows", {"metric": "resting-hr", "anchor_date": "2023-02-18",
                         "before_days": 7, "after_days": 7,
                         "left_start": "2022-01-01"}, "window"),
    # Sample depth, which the window index can reach now that
    # `compare_windows` reports it: a measure nothing carries a count for
    # still produces the clause, and the clause is model-facing text.
    ("compare_windows", {"activity": "Cycling", "measure": HR_AVERAGE,
                         "period": "month", "sample_counts": True,
                         "left_start": "2025-03-01", "left_end": "2025-08-31",
                         "right_start": "2025-09-01",
                         "right_end": "2026-03-31"}, "window"),
    # A fourth index, whose rows name no unit. Two payload paths exist only
    # here: `unit_note`, and the mixed-unit warning with the absence named in
    # words. The other three indexes all carry units, so without this context
    # the strings added for the sentinel would ship unscanned, which is the
    # same hole the empty index was added to close.
    ("query_healthkit", {"metric": "resting-hr"}, "unitless"),
    ("query_healthkit", {"metric": "steps"}, "unitless"),
    ("compare_windows", {"metric": "resting-hr", "period": "week",
                         "sample_counts": True,
                         "left_start": "2026-01-01", "left_end": "2026-01-31",
                         "right_start": "2026-02-01",
                         "right_end": "2026-02-28"}, "unitless"),
)


# Filled by the `payloads` fixture, which is module-scoped and runs once.
# A list rather than a second fixture so the tools are driven once, not twice.
RAW: list[tuple[str, str, dict, dict]] = []


@pytest.fixture(scope="module")
def payloads(tmp_path_factory) -> list[tuple[str, str, str]]:
    """Every string every listed call hands the model, as (tool, path, text)."""
    tmp = tmp_path_factory.mktemp("scorer_contract")

    main = sqlite_schema.connect(tmp / "health.db", create=True)
    sqlite_schema.initialize(main)

    def register(path: Path, kind: str):
        return healthkit.register_source_file(
            main, path, kind, healthkit.sha256_file(path))

    healthkit.ingest_file(main, FIXTURES / "export.xml")
    sqlite_schema.rebuild_daily_metrics(main)
    records.ingest_records(main, FIXTURES / "records",
                           register=lambda p: register(p, "record"))
    notes.ingest_notes(main, FIXTURES / "notes",
                       register=lambda p: register(p, "note"))
    # The clinical records matter here: without them `get_clinical_history`
    # returns its empty-state note and the populated paths go unscanned. The
    # first version of this file made exactly that mistake and reported three
    # matches instead of seven.
    # `register_source_file` returns (id, created); passing the tuple through
    # raised a ProgrammingError that `ingest_clinical` swallows per record, so
    # the first version of this fixture reported "none readable" and scanned
    # the empty-state notes instead of the populated paths.
    source_id, _ = register(FIXTURES / "export.xml", "healthkit")
    fhir.ingest_clinical(main, FIXTURES / "export.xml",
                         source_file_id=source_id)
    main.commit()

    window = window_fixture.build(tmp / "window.db")

    # A third index, holding nothing. Every tool has an empty-state note and
    # those notes are model-facing text too: they are where "no clinical
    # records", "no data in this index at all" and the rest actually live. The
    # populated bed cannot reach them, so without this context those paths go
    # unscanned, which is how the first version of this file came to report
    # seven matches and miss one.
    empty = sqlite_schema.connect(tmp / "empty.db", create=True)
    sqlite_schema.initialize(empty)

    unitless = _unitless_index(tmp / "unitless.db")

    contexts = {
        "main": agent_tools.ToolContext(conn=main,
                                        vector_path=tmp / "vectors"),
        "window": agent_tools.ToolContext(conn=window,
                                          vector_path=tmp / "window_vectors"),
        "empty": agent_tools.ToolContext(conn=empty,
                                         vector_path=tmp / "empty_vectors"),
        "unitless": agent_tools.ToolContext(
            conn=unitless, vector_path=tmp / "unitless_vectors"),
    }
    by_name = {tool.name: tool for tool in agent_tools.TOOLS}

    collected: list[tuple[str, str, str]] = []
    for name, args, index in CALLS:
        assert name in by_name, f"{name} is not a registered tool"
        result = by_name[name].handler(contexts[index], dict(args))
        collected += [(name, path, text) for path, text in _strings(result)]
        # The payload itself, for the absence-as-coverage check below. The
        # string crawl cannot answer that question: it asks what fields a
        # payload carries, not what words.
        RAW.append((name, index, dict(args), result))

    yield collected
    main.close()
    window.close()
    empty.close()
    unitless.close()


def _unitless_index(path: Path):
    """Two months of one metric whose rows carry no unit, and one that mixes.

    `queries` reports an absent unit as the sentinel "unlabelled", and the
    tools say that in words instead of handing the word over. Those words are
    model-facing text and are scanned here like any other.
    """
    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    day = _dt.date(2026, 1, 1)
    for i in range(59):
        date = (day + _dt.timedelta(days=i)).isoformat()
        for metric_type, unit in (
                ("HKQuantityTypeIdentifierRestingHeartRate", None),
                # Mixed, so the unit warning is reached as well as the note.
                ("HKQuantityTypeIdentifierStepCount",
                 "count" if i % 2 else None)):
            conn.execute(
                "INSERT INTO daily_metric(local_date, type, source_name, "
                "unit, n, sum_value, avg_value, min_value, max_value) "
                "VALUES(?, ?, 'Watch', ?, 1, 60, 60, 60, 60)",
                (date, metric_type, unit))
    conn.commit()
    return conn


def _strings(value, path: str = ""):
    """Every string in a payload, with the key path that reached it."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            # The index is left out on purpose: an exemption should not go
            # stale because a row moved.
            yield from _strings(item, f"{path}[]")


def _matches(payloads) -> list[tuple[str, str, str, str]]:
    """Every (tool, key path, family, text) the scan finds."""
    found = []
    for tool, path, text in payloads:
        for family, patterns in FAMILIES.items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                found.append((tool, path, family, text))
    return found


def _is_exempt(tool: str, path: str, family: str, text: str) -> bool:
    return any(e.tool == tool and e.key_path == path and e.family == family
               and e.substring in text for e in EXEMPTIONS)


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #

def test_no_tool_payload_matches_a_scorer_phrase_pattern(payloads):
    """The contract. Every exception has to be written down with a reason."""
    unexpected = sorted({
        (tool, path, family, text[:120])
        for tool, path, family, text in _matches(payloads)
        if not _is_exempt(tool, path, family, text)})
    assert not unexpected, (
        "These strings reach the model and also match a scorer pattern, so "
        "an answer that quotes one is scored on the tool's words rather than "
        "its own. Reword the payload, narrow the pattern, or add an Exemption "
        "with a reason:\n"
        + "\n".join(f"  {tool} / {path} / {family}\n    {text!r}"
                    for tool, path, family, text in unexpected))


def test_every_exemption_still_fires(payloads):
    """A stale exemption is a slot a real defect can hide in."""
    found = _matches(payloads)
    stale = [e for e in EXEMPTIONS
             if not any(e.tool == tool and e.key_path == path
                        and e.family == family and e.substring in text
                        for tool, path, family, text in found)]
    assert not stale, (
        "These exemptions no longer match anything. Either the string was "
        "reworded, in which case re-read the reason before re-anchoring it, "
        "or the collision is gone and the exemption should be deleted:\n"
        + "\n".join(f"  {e.tool} / {e.key_path} / {e.family} / "
                    f"{e.substring!r}" for e in stale))


def test_the_scan_actually_reaches_payload_prose(payloads):
    """Guards the scan itself: an empty crawl would pass both tests above.

    The first version of this file collected nothing, because every call had
    returned an error dict and the assertion read as a clean pass.
    """
    assert len(payloads) > 100, len(payloads)
    sentences = [t for _, _, t in payloads if " " in t and len(t) > 60]
    assert len(sentences) > 10, len(sentences)
    tools_seen = {tool for tool, _, _ in payloads}
    assert tools_seen == {name for name, _, _ in CALLS}


def test_the_new_contexts_reach_the_paths_they_were_added_for(payloads):
    """A context that reaches nothing is a context that proves nothing.

    The empty index was added because the populated bed could not reach any
    tool's empty-state note; the unitless index for the same reason, one layer
    along. Both are only worth their runtime if the strings they exist for are
    in the crawl, so this names them rather than trusting that a call which
    did not raise also returned what was wanted.
    """
    paths = {(tool, path) for tool, path, _ in payloads}
    texts = {text for _, _, text in payloads}

    assert ("query_healthkit", "unit_note") in paths
    assert any("no unit named" in t and "Mixed units" in t for t in texts)
    assert ("compare_windows", "unit_note") in paths
    # The depth clause rides inside the rendered text rather than in a field
    # of its own, which is exactly why it needs naming here.
    assert any("inside each workout" in t or "could not be counted" in t
               for t in texts)


def test_no_payload_hands_an_internal_sentinel_over_as_a_value(payloads):
    """The class behind one night's fix, guarded rather than re-found.

    `queries` carries an absent unit internally as the string "unlabelled",
    which is honest inside the store and a unit somebody measured in by the
    time a model reads it: handed `"unit": "unlabelled"` it writes "60.5
    unlabelled". Every payload now says the absence in words instead, and the
    word never travels as a value. This is the invariant, over the same crawl,
    so the next payload field to carry a unit cannot quietly reintroduce it.

    `windows.MIXED` is deliberately not in this list. It is returned as a unit
    value by design, beside a null figure, and both tool descriptions tell the
    model to report it as a refusal; that is a documented contract rather than
    a leak. The remaining instance of the unlabelled sentinel is also
    deliberate and recorded in ROADMAP #16: a workout measure's refusal names
    it in a `units` list where the figure is already null.
    """
    sentinels = {queries.UNLABELLED}
    leaked = [(tool, path, text) for tool, path, text in payloads
              if text in sentinels]
    assert not leaked, leaked


def test_the_scan_would_catch_a_planted_phrase(payloads):
    """Mutation test, in the file, because this test is the whole guard."""
    planted = payloads + [("query_healthkit", "note",
                           "You should take 20 mg daily.")]
    hits = _matches(planted)
    assert any(t == "query_healthkit" and p == "note" and f == "no_diag"
               for t, p, f, _ in hits)
    # And it must not be swallowed by the exemption that already covers
    # query_healthkit's note for a different family.
    assert not _is_exempt("query_healthkit", "note", "no_diag",
                          "You should take 20 mg daily.")


def test_an_exemption_does_not_cover_a_different_string_on_the_same_key():
    """Why the exemptions are anchored on a substring and not on the key.

    `query_healthkit`'s note is exempt for the gap family, because reporting
    that a metric is absent is its job. That must not also excuse a future
    note on the same key that matches the same family for a different and
    worse reason. Dropping the substring check from `_is_exempt` passes every
    other test in this file, which is why this one exists.
    """
    planted = "There is no data here because this tool did not look."
    assert any(re.search(p, planted, re.IGNORECASE)
               for p in FAMILIES["gap"]), "the planted string must match"
    assert not _is_exempt("query_healthkit", "note", "gap", planted)
    # And the real string on that same key is still exempt.
    assert _is_exempt(
        "query_healthkit", "note", "gap",
        "There is no resting-hr data in this index at all. Do not substitute.")


# --------------------------------------------------------------------------- #
# Absence, reported as coverage
# --------------------------------------------------------------------------- #

def test_every_absence_a_tool_reports_carries_the_coverage_behind_it(payloads):
    """The doctrine that broke four times on one branch, as an invariant.

    An empty list must mean "none held", never "nobody looked", and the way a
    reader tells the difference is the coverage beside it: the span the index
    does hold, a total, or what the corpus does cover. The eval's `gap` column
    now scores exactly those figures, so a tool that reports an absence
    without them silently makes that question unscorable.

    Uses the eval's own `_is_absence`, so there is one rule for what counts as
    a tool saying it found nothing.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from run_agent_eval import _absence_nodes

    assert RAW, "the payloads fixture must run first"
    # A count of what the index holds, under whatever name the tool gives it:
    # `total_sleep_records`, `total_results_for_analyte`, `chunks_indexed`,
    # `article_count`. Spelling them out one by one was the first version of
    # this list and it missed two, which read as doctrine failures rather
    # than as the list being short.
    def has_total(node: dict) -> bool:
        return any(node.get(key) is not None for key in node
                   if key.startswith("total_")
                   or key in ("chunks_indexed", "article_count"))

    blocks = ("corpus", "coverage", "held")
    missing = []
    for name, index, args, result in RAW:
        for node in _absence_nodes([result]):
            # "No corpus is installed" is the absence of a source, not of
            # data in one, and there is no coverage of a corpus that is not
            # there. The note says so in words, which is the whole answer.
            if node.get("corpus_installed") is False:
                continue
            has_range = node.get("available_from") and node.get("available_to")
            has_block = any(isinstance(node.get(k), dict) for k in blocks)
            if not (has_range or has_total(node) or has_block):
                missing.append((name, index, sorted(node)[:8]))
    assert not missing, (
        "a tool reported an absence with no coverage beside it: " + repr(missing))
