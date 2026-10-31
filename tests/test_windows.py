"""Windows either side of a dated event, and the verdict on whether two of
them can be compared at all.

T7's refusal is the point of this module: a tool that compares two windows
will be asked to compare windows that cannot be compared.

The word scans in `test_every_verdict_is_scanned_for_a_claim_it_cannot_carry`
are the real control here, not the guardrail run beside them: the guardrail's
patterns are anchored on clinical markers and condition names, and it holds
no pattern for cause, for a percentage or for a gap between two spans.
"""
from __future__ import annotations

import pytest

from pathlib import Path as _Path

from health_agent.agent import guardrail
from health_agent.store import queries, sqlite_schema, windows

_TESTS_DIR = _Path(__file__).parent

HR = "HKQuantityTypeIdentifierHeartRate"
HR_AVG = f"{HR}:average"
CADENCE_AVG = "HKQuantityTypeIdentifierCyclingCadence:average"

# Words a comparison of two spans must never produce: the gap between the
# sides, a direction read into it, or a verdict on what it means.
FORBIDDEN = ("difference", "delta", "change", "percent", "pct", "%", "rise",
             "fall", "increase", "decrease", "worse", "better", "improved",
             "significant", "likely", "suggests", "caused by", "led to",
             "resulted in")


@pytest.fixture
def box(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def _daily(conn, *, date, value, metric_type="HKQuantityTypeIdentifierRestingHeartRate",
           unit="count/min", source="Watch"):
    conn.execute(
        "INSERT INTO daily_metric(local_date, type, source_name, unit, n, "
        "sum_value, avg_value, min_value, max_value) "
        "VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (date, metric_type, source, unit, value, value, value, value))
    conn.commit()


def _sleep(conn, *, date, category, seconds):
    conn.execute(
        "INSERT INTO daily_metric(local_date, type, source_name, unit, "
        "category, n, duration_sec) "
        "VALUES(?, 'HKCategoryTypeIdentifierSleepAnalysis', 'Watch', NULL, "
        "?, 1, ?)", (date, category, seconds))
    conn.commit()


def _document(conn):
    """clinical_fact.document_id is NOT NULL and `clinical_facts` joins
    document for the citation, so a fact needs a document behind it."""
    cur = conn.execute(
        "INSERT INTO source_file(path, kind, sha256, size_bytes, ingested_at) "
        "VALUES('export.zip', 'healthkit', 'abc', 1, '2026-10-22')")
    cur = conn.execute(
        "INSERT INTO document(source_file_id, path, kind, page_count, "
        "extraction, ingested_at) VALUES(?, 'clinical-records/a.json', 'fhir', "
        "1, 'text', '2026-10-22')", (cur.lastrowid,))
    conn.commit()
    return cur.lastrowid


def _fact(conn, *, name, date, kind="procedure", key=b"\x01", document_id=None):
    doc = document_id if document_id is not None else _document(conn)
    conn.execute(
        "INSERT INTO clinical_fact(document_id, kind, name, status, "
        "onset_date, dedup_key) VALUES(?, ?, ?, 'completed', ?, ?)",
        (doc, kind, name, date, key))
    conn.commit()
    return doc


def _ride(conn, *, date, key, hr_avg=140.0, hr_unit="count/min", duration=40.0,
          duration_unit="min", distance=10.0, distance_unit="mi",
          source="Watch", indoor=None,
          activity="HKWorkoutActivityTypeCycling"):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, source_name, start_date, "
        "start_epoch, local_date, indoor, dedup_key) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 1772830800.0, ?, ?, ?)",
        (activity, duration, duration_unit, distance, distance_unit, source,
         f"{date}T09:00:00-05:00", date, indoor, key))
    if hr_avg is not None:
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, ?, ?)",
            (cur.lastrowid, HR_AVG, hr_avg, hr_unit))
    conn.commit()


# --------------------------------------------------------------------------- #
# The anchor
# --------------------------------------------------------------------------- #

def test_an_explicit_date_resolves_to_itself(box):
    anchor = windows.resolve_anchor(box, date="2023-02-18")
    assert anchor.date == "2023-02-18"
    assert anchor.citation is None


def test_an_anchor_can_be_a_dated_row_the_index_already_holds(box):
    _fact(box, name="ACL reconstruction", date="2023-09-15")
    anchor = windows.resolve_anchor(box, fact="ACL")
    assert anchor.date == "2023-09-15"
    assert "ACL reconstruction" in anchor.label
    assert anchor.citation == "clinical-records/a.json"


def test_an_ambiguous_anchor_returns_every_candidate_and_chooses_nothing(box):
    """Guessing which of two procedures someone meant is the same failure as
    reporting an empty window as an empty record."""
    doc = _document(box)
    for i, date in enumerate(("2023-09-15", "2024-03-11")):
        _fact(box, name="knee arthroscopy", date=date, key=bytes([i + 1]),
              document_id=doc)
    result = windows.resolve_anchor(box, fact="knee")
    assert isinstance(result, windows.AnchorAmbiguous)
    assert len(result.candidates) == 2
    assert {c["date"] for c in result.candidates} == {"2023-09-15", "2024-03-11"}


def test_two_rows_on_one_date_are_one_anchor_not_an_ambiguity(box):
    """Ambiguity is about which date was meant. Two rows that name the same
    date give one window either way, so there is nothing to choose."""
    doc = _document(box)
    for i, name in enumerate(("knee arthroscopy", "knee debridement")):
        _fact(box, name=name, date="2023-09-15", key=bytes([i + 1]),
              document_id=doc)
    anchor = windows.resolve_anchor(box, fact="knee")
    assert isinstance(anchor, windows.Anchor)
    assert anchor.date == "2023-09-15"
    assert "knee arthroscopy" in anchor.label
    assert "knee debridement" in anchor.label


def test_an_anchor_that_matches_nothing_is_none(box):
    assert windows.resolve_anchor(box, fact="nothing like this") is None


def test_an_anchor_with_nothing_to_resolve_is_a_caller_error_not_a_none(box):
    """Nothing asked for and nothing found are two different absences."""
    with pytest.raises(ValueError):
        windows.resolve_anchor(box)


def test_an_unparseable_anchor_date_fails_at_the_anchor(box):
    with pytest.raises(ValueError):
        windows.resolve_anchor(box, date="last Tuesday")


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

def test_a_window_summarizes_each_side_and_counts_the_days_with_no_data(box):
    for day, value in (("2023-02-11", 63.0), ("2023-02-12", 63.0),
                       ("2023-02-19", 80.0), ("2023-02-20", 90.0)):
        _daily(box, date=day, value=value)
    spec = windows.SeriesSpec(metric="resting-hr")
    result = windows.window(box, spec=spec, anchor=windows.resolve_anchor(
        box, date="2023-02-18"), before_days=7, after_days=6)
    assert result.before.n == 2
    assert result.before.max == 63.0
    assert result.after.n == 2
    assert result.after.max == 90.0
    assert result.before.empty_periods == 5
    assert result.after.empty_periods == 4


def test_the_day_of_the_event_belongs_to_neither_side(box):
    """The anchor day is the event, not a before day or an after day."""
    for day in ("2023-02-17", "2023-02-18", "2023-02-19"):
        _daily(box, date=day, value=70.0)
    result = windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-18"),
        before_days=1, after_days=1)
    assert (result.before.first, result.before.last) == ("2023-02-17",
                                                         "2023-02-17")
    assert (result.after.first, result.after.last) == ("2023-02-19",
                                                       "2023-02-19")


def test_the_excluded_anchor_day_is_named_rather_than_dropped_in_silence(box):
    """A day the tool set aside is an absence the tool created, so it is
    reported rather than left for the reader to notice."""
    for day in ("2023-02-17", "2023-02-18", "2023-02-19"):
        _daily(box, date=day, value=70.0)
    comparison = windows.as_comparison(windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-18"),
        before_days=1, after_days=1))
    assert comparison.comparable["anchor_day"] == "2023-02-18"
    text = windows.render(comparison)
    assert "2023-02-18" in text and "neither side" in text


def test_a_range_comparison_names_no_anchor_day(box):
    _daily(box, date="2023-01-01", value=60.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert "anchor_day" not in comparison.comparable
    assert "neither side" not in windows.render(comparison)


def test_the_anchored_note_names_the_anchor_in_one_place(box):
    _daily(box, date="2023-02-17", value=70.0)
    comparison = windows.as_comparison(windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-18"),
        before_days=1, after_days=1))
    assert "2023-02-18" in comparison.note
    assert "no claim" in comparison.note


def test_a_window_hands_back_a_comparison_through_a_public_name(box):
    for day, value in (("2023-02-11", 63.0), ("2023-02-19", 90.0)):
        _daily(box, date=day, value=value)
    result = windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-18"),
        before_days=7, after_days=7)
    comparison = windows.as_comparison(result)
    assert isinstance(comparison, windows.Comparison)
    assert comparison.left == result.before
    assert comparison.right == result.after
    assert comparison.comparable["verdict"] in windows.VERDICTS
    assert "2023-02-18" in comparison.label


def test_a_bucketed_span_is_clipped_to_the_window_it_was_asked_for(box):
    """A week bucket runs past the window at both ends. Unclipped, the after
    side appears to start before the anchor and both sides show one span."""
    for day in ("2023-02-06", "2023-02-07", "2023-02-09", "2023-02-10"):
        _daily(box, date=day, value=70.0)
    result = windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-08"),
        before_days=7, after_days=7, period="week")
    assert result.before.last == "2023-02-07"
    assert result.after.first == "2023-02-09"
    assert result.before.window_start == "2023-02-01"
    assert result.after.window_end == "2023-02-15"
    rendered = windows.render(windows.as_comparison(result))
    assert "2023-02-07" in rendered and "2023-02-09" in rendered


def test_an_unknown_metric_errors_rather_than_coming_back_empty(box):
    with pytest.raises(ValueError):
        windows.window(box, spec=windows.SeriesSpec(metric="not-a-metric"),
                       anchor=windows.resolve_anchor(box, date="2023-02-18"),
                       before_days=7, after_days=7)


def test_a_category_metric_is_refused_rather_than_summarized_across_stages(box):
    """A sleep series carries one point per stage per period. Summarizing it
    as it comes reports the spread across stages as a range and one night as
    three periods, which defeats the thin rule and can read as a false ok."""
    for category, seconds in (("Core", 900.0), ("REM", 1800.0),
                              ("Deep", 3600.0)):
        _sleep(box, date="2023-01-01", category=category, seconds=seconds)
    for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
        _sleep(box, date=day, category="Core", seconds=1800.0)
    with pytest.raises(ValueError) as caught:
        windows.compare(box, spec=windows.SeriesSpec(metric="sleep"),
                        left=("2023-01-01", "2023-01-31"),
                        right=("2024-01-01", "2024-01-31"))
    assert "category" in str(caught.value)


# --------------------------------------------------------------------------- #
# What the spec passes through to the layer below
# --------------------------------------------------------------------------- #

def test_a_derivation_is_read_from_the_periods_derived_figures(box):
    """The derived figure is what the spec named, not the first measure that
    happens to carry a median."""
    _ride(box, date="2023-01-01", key=b"\x01", distance=10.0, duration=40.0)
    _ride(box, date="2023-01-02", key=b"\x02", distance=8.0, duration=40.0)
    _ride(box, date="2024-01-01", key=b"\x03", distance=10.0, duration=45.0)
    _ride(box, date="2024-01-02", key=b"\x04", distance=10.0, duration=55.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", derive_name="pace"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.left.median == 4.5          # 4.0 and 5.0 min/mi
    assert comparison.right.median == 5.0         # 4.5 and 5.5 min/mi
    assert comparison.left.unit == "min/mi"
    assert comparison.comparable["verdict"] == "ok"


def test_a_derivation_a_spec_cannot_pin_down_is_refused_by_name(box):
    for name in ("ratio", "percentile"):
        with pytest.raises(ValueError) as caught:
            windows.compare(
                box, spec=windows.SeriesSpec(activity="Cycling",
                                             derive_name=name),
                left=("2023-01-01", "2023-01-31"),
                right=("2024-01-01", "2024-01-31"))
        assert name in str(caught.value)


def test_the_indoor_filter_reaches_the_series(box):
    _ride(box, date="2023-01-01", key=b"\x01", indoor=1)
    _ride(box, date="2023-01-02", key=b"\x02", indoor=0)
    _ride(box, date="2024-01-01", key=b"\x03", indoor=0)
    ranges = dict(left=("2023-01-01", "2023-01-31"),
                  right=("2024-01-01", "2024-01-31"))
    both = windows.compare(box, spec=windows.SeriesSpec(activity="Cycling"),
                           **ranges)
    outdoor = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", indoor=False),
        **ranges)
    assert both.left.n == 2 and outdoor.left.n == 1


def test_a_source_exclusion_reaches_the_series_and_is_named_when_it_empties_a_side(box):
    _daily(box, date="2023-01-01", value=60.0, source="Watch")
    _daily(box, date="2024-01-01", value=61.0, source="Phone")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr",
                                     source_exclude=("Phone",)),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.left.n == 1 and comparison.right.n == 0
    because = comparison.comparable["because"]
    assert "Phone" in because and "excluded" in because
    assert "Phone" in windows.render(comparison)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def test_two_windows_whose_ranges_never_overlap_are_not_comparable(box):
    """T7: the pace ranges never overlap, so no like-for-like comparison is
    available. A rule, not a judgment."""
    for day, value in (("2023-01-01", 60.0), ("2023-01-02", 62.0)):
        _daily(box, date=day, value=value)
    for day, value in (("2024-01-01", 90.0), ("2024-01-02", 95.0)):
        _daily(box, date=day, value=value)
    spec = windows.SeriesSpec(metric="resting-hr")
    comparison = windows.compare(
        box, spec=spec, left=("2023-01-01", "2023-01-31"),
        right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "no_overlap"
    assert comparison.comparable["left_range"] == [60.0, 62.0]
    assert comparison.comparable["right_range"] == [90.0, 95.0]


def test_a_single_period_is_thin_rather_than_a_statement_about_ranges(box):
    """One period is a point, not a range. A range claim over a point reads
    as more than it knows, so the thin rule runs first."""
    _daily(box, date="2023-01-01", value=60.0)
    _daily(box, date="2024-01-01", value=95.0)
    one_each = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert one_each.comparable["verdict"] == "thin_on_one_side"
    for day, value in (("2023-01-02", 62.0), ("2023-01-03", 64.0)):
        _daily(box, date=day, value=value)
    three_against_one = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert three_against_one.comparable["verdict"] == "thin_on_one_side"
    assert three_against_one.comparable["right_n"] == 1
    assert "1 period" in three_against_one.comparable["because"]


def test_a_comparison_never_returns_the_gap_between_its_two_sides(box):
    for day, value in (("2023-01-01", 60.0), ("2024-01-01", 62.0)):
        _daily(box, date=day, value=value)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    flat = str(comparison.comparable) + str(comparison.left) + str(comparison.right)
    for forbidden in FORBIDDEN:
        assert forbidden not in flat.lower()


def test_an_empty_side_is_insufficient_data_rather_than_a_verdict(box):
    _daily(box, date="2023-01-01", value=60.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2025-01-01", "2025-01-31"))
    assert comparison.comparable["verdict"] == "insufficient_data"


def test_an_empty_side_reports_what_the_index_holds(box):
    """Absence is reported as coverage, and an empty range is a different
    absence from an index that holds nothing of this figure at all."""
    _daily(box, date="2023-01-01", value=60.0)
    out_of_range = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2025-01-01", "2025-01-31"))
    nothing_at_all = windows.compare(
        box, spec=windows.SeriesSpec(metric="hrv"),
        left=("2023-01-01", "2023-01-31"), right=("2025-01-01", "2025-01-31"))
    assert out_of_range.comparable["verdict"] == "insufficient_data"
    assert nothing_at_all.comparable["verdict"] == "insufficient_data"
    assert (out_of_range.comparable["because"]
            != nothing_at_all.comparable["because"])
    assert "2023-01-01" in out_of_range.comparable["because"]
    # One record, named in the singular.
    assert "1 record" in out_of_range.comparable["because"]
    assert out_of_range.comparable["right_coverage"]["total_records"] == 1
    assert nothing_at_all.comparable["right_coverage"]["total_records"] == 0
    assert "2023-01-01" not in nothing_at_all.comparable["because"]
    # The wording itself, not just the absence of dates: with this branch
    # gone the sentence reads "holds 0 records of this figure from None to
    # None", which is the two-absences-in-one-representation output.
    assert ("holds no records of this figure at all"
            in nothing_at_all.comparable["because"])
    assert "None" not in windows.render(nothing_at_all)


def test_an_empty_workout_side_counts_workouts_and_says_so(box):
    """A metric side counts records and a workout side counts workouts. One
    unlabelled number for both was a count of nothing in particular."""
    _ride(box, date="2023-01-01", key=b"\x01")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.right.counted == "workouts"
    assert comparison.comparable["right_coverage"]["total_workouts"] == 1
    assert "1 workout" in comparison.comparable["because"]
    assert "1 records" not in comparison.comparable["because"]


def test_an_activity_the_index_does_not_hold_names_the_ones_it_does(box):
    _ride(box, date="2023-01-01", key=b"\x01")
    _ride(box, date="2023-01-02", key=b"\x02")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Swimming"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    because = comparison.comparable["because"]
    assert "no workout named like Swimming" in because
    assert "Cycling (2 workouts)" in because


def test_a_measure_no_workout_carries_names_what_is_carried(box):
    """Periods present and no figure in them, with the measures the index
    does hold named: three absences that must not read alike."""
    _ride(box, date="2023-01-01", key=b"\x01", hr_avg=140.0)
    _ride(box, date="2024-01-01", key=b"\x02", hr_avg=141.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=CADENCE_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "missing_measure_on_one_side"
    assert comparison.comparable["left_n"] == 1
    assert comparison.comparable["left_value_periods"] == 0
    because = comparison.comparable["because"]
    assert f"no workout carries {CADENCE_AVG}" in because
    assert HR_AVG in because
    assert f"no workout carries {CADENCE_AVG}" in windows.render(comparison)


def test_sides_in_different_units_are_never_called_comparable(box):
    _daily(box, date="2023-01-01", value=60.0, unit="count/min")
    _daily(box, date="2024-01-01", value=61.0, unit="bpm")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mismatch"


def test_one_side_holding_two_units_is_its_own_refusal(box):
    """A side whose own range mixes units was already refused below this
    layer. It is not two sides that disagree, so it is not unit_mismatch."""
    _daily(box, date="2023-01-01", value=60.0, unit="count/min")
    _daily(box, date="2023-01-05", value=61.0, unit="bpm")
    _daily(box, date="2024-01-01", value=62.0, unit="count/min")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"
    assert comparison.comparable["left_units_seen"] == ("bpm", "count/min")


def test_a_mixed_unit_sentinel_from_the_layer_below_is_not_comparable(box):
    """`workout_series` writes unit "mixed" when a period holds more than one
    unit for a figure. Two "mixed" sides compare equal, so a plain unit
    equality test would read them as comparable."""
    _ride(box, date="2023-01-01", key=b"\x01", hr_avg=140.0,
          hr_unit="count/min")
    _ride(box, date="2023-01-01", key=b"\x02", hr_avg=150.0, hr_unit="bpm")
    _ride(box, date="2024-01-01", key=b"\x03", hr_avg=141.0,
          hr_unit="count/min")
    _ride(box, date="2024-01-01", key=b"\x04", hr_avg=151.0, hr_unit="bpm")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"


def test_the_mixed_side_names_its_own_units_and_never_the_other_sides(box):
    """The offending side's units are the only evidence that it mixes. A
    union with the other side offers one unit as proof of mixing, and that
    unit belongs to the side that is not mixed.

    The later side's unit is deliberately one the mixed side does not hold:
    with a unit the mixed side already has, a union produces the identical
    sentence and this test cannot tell the two apart.
    """
    _ride(box, date="2023-01-01", key=b"\x01", distance=10.0,
          distance_unit="mi")
    _ride(box, date="2023-01-02", key=b"\x02", distance=10.0,
          distance_unit="km")
    _ride(box, date="2024-01-01", key=b"\x03", distance=10.0,
          distance_unit="ft")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", derive_name="pace"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"),
        period="month")
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"
    because = comparison.comparable["because"]
    assert "the earlier side" in because
    assert "min/km" in because and "min/mi" in because
    assert "min/ft" not in because        # a union would have added it
    assert "the later side" not in because
    assert comparison.right.units_seen == ("min/ft",)
    # The sentence stands on the mixed side's own units, which the series
    # now carries on its derived entries as well as on its measures.
    assert comparison.left.units_seen == ("min/km", "min/mi")


def test_overlapping_sides_with_enough_periods_are_comparable(box):
    for day, value in (("2023-01-01", 60.0), ("2023-01-02", 64.0),
                       ("2023-01-03", 68.0)):
        _daily(box, date=day, value=value)
    for day, value in (("2024-01-01", 62.0), ("2024-01-02", 66.0),
                       ("2024-01-03", 70.0)):
        _daily(box, date=day, value=value)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "ok"
    assert comparison.left.median == 64.0
    assert comparison.right.median == 66.0


def test_sample_counts_adds_the_counts_under_their_own_noun(box):
    for day, value in (("2023-01-01", 60.0), ("2023-01-02", 64.0),
                       ("2023-01-03", 68.0)):
        _daily(box, date=day, value=value)
    for day, value in (("2024-01-01", 62.0), ("2024-01-02", 66.0),
                       ("2024-01-03", 70.0)):
        _daily(box, date=day, value=value)
    kwargs = dict(spec=windows.SeriesSpec(metric="resting-hr"),
                  left=("2023-01-01", "2023-01-31"),
                  right=("2024-01-01", "2024-01-31"))
    plain = windows.compare(box, **kwargs)
    counted = windows.compare(box, sample_counts=True, **kwargs)
    assert "left_records" not in plain.comparable
    assert counted.comparable["left_records"] == 3
    assert counted.comparable["right_records"] == 3
    assert counted.left.counted == "records"
    assert counted.comparable["verdict"] == plain.comparable["verdict"]


def test_a_workout_side_counts_workouts_when_counts_are_asked_for(box):
    for day, key in (("2023-01-01", b"\x01"), ("2023-01-02", b"\x02")):
        _ride(box, date=day, key=key)
    for day, key in (("2024-01-01", b"\x03"), ("2024-01-02", b"\x04")):
        _ride(box, date=day, key=key)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"),
        sample_counts=True)
    assert comparison.comparable["left_workouts"] == 2
    assert "left_records" not in comparison.comparable


# --------------------------------------------------------------------------- #
# The rendered text, over every verdict
# --------------------------------------------------------------------------- #

def _one_of_each_verdict(box):
    """One comparison per verdict, from one index mutated in place between
    them, in an order that reaches every rule."""
    spec = windows.SeriesSpec(metric="resting-hr")
    left, right = ("2023-01-01", "2023-01-31"), ("2024-01-01", "2024-01-31")

    # insufficient_data: nothing anywhere.
    yield windows.compare(box, spec=spec, left=left, right=right)
    for day, value in (("2023-01-01", 60.0), ("2023-01-02", 64.0),
                       ("2023-01-03", 68.0)):
        _daily(box, date=day, value=value)
    # thin_on_one_side: one period on the right.
    _daily(box, date="2024-01-01", value=66.0)
    yield windows.compare(box, spec=spec, left=left, right=right)
    # ok: three overlapping periods each side.
    for day, value in (("2024-01-02", 62.0), ("2024-01-03", 70.0)):
        _daily(box, date=day, value=value)
    yield windows.compare(box, spec=spec, left=left, right=right)
    # no_overlap: push the right side clear of the left.
    for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
        box.execute("DELETE FROM daily_metric WHERE local_date = ?", (day,))
    for day, value in (("2024-01-01", 90.0), ("2024-01-02", 95.0),
                       ("2024-01-03", 99.0)):
        _daily(box, date=day, value=value)
    yield windows.compare(box, spec=spec, left=left, right=right)
    # unit_mismatch: the right side in another unit.
    box.execute("UPDATE daily_metric SET unit = 'bpm' WHERE local_date >= "
                "'2024-01-01'")
    box.commit()
    yield windows.compare(box, spec=spec, left=left, right=right)
    # unit_mixed_on_one_side: one side holding both units.
    box.execute("UPDATE daily_metric SET unit = 'bpm' WHERE local_date = "
                "'2023-01-01'")
    box.commit()
    yield windows.compare(box, spec=spec, left=left, right=right)
    # missing_measure_on_one_side: periods with no value for the measure.
    _ride(box, date="2023-01-01", key=b"\x01", hr_avg=140.0)
    _ride(box, date="2024-01-01", key=b"\x02", hr_avg=141.0)
    yield windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling",
                                     measure=CADENCE_AVG),
        left=left, right=right)
    # Every branch again with an anchor, so the anchor-day line is scanned.
    _fact(box, name="knee surgery", date="2023-01-02")
    yield windows.as_comparison(windows.window(
        box, spec=spec, anchor=windows.resolve_anchor(box, fact="knee"),
        before_days=30, after_days=30))


def test_every_verdict_is_scanned_for_a_claim_it_cannot_carry(box):
    """The real control on the template. Every verdict's own `because`, both
    side lines and the rendered text are scanned, because a word that reads a
    gap into two spans is the failure this module exists to avoid.

    The guardrail runs here too, as a cheap regression guard and nothing
    more: it holds no pattern for cause, for a percentage or for a comparison
    between two spans.
    """
    seen = set()
    for comparison in _one_of_each_verdict(box):
        verdict = comparison.comparable["verdict"]
        seen.add(verdict)
        text = windows.render(comparison)
        assert verdict in text
        flat = (text + str(comparison.comparable) + str(comparison.left)
                + str(comparison.right) + comparison.note).lower()
        for forbidden in FORBIDDEN:
            assert forbidden not in flat, (verdict, forbidden, flat)
        flags = guardrail.check(text, used_tools=True,
                                returned_pmids=frozenset())
        assert not flags, (text, flags)
    assert seen == set(windows.VERDICTS)
    assert len(set(windows.VERDICTS)) == len(windows.VERDICTS)


def test_the_module_reads_through_queries_and_not_its_own_sql(box):
    """The one-read-path rule: the only SQL in this module is the anchor
    lookup, and that goes through `queries.clinical_facts` too."""
    import inspect

    source = inspect.getsource(windows)
    for keyword in ("SELECT", "INSERT", "UPDATE", "DELETE", "conn.execute"):
        assert keyword not in source, keyword
    assert queries.clinical_facts.__name__ in source


def test_a_mixed_figure_the_layer_below_did_not_name_says_so(box):
    """The duration figure is summed by `_summed`, which returns the "mixed"
    sentinel without a list of the units that clashed. Naming units it does
    not have would mean borrowing them, so the sentence says the layer below
    did not name them. Reachable from a plain SeriesSpec, which is why it is
    tested rather than explained away."""
    _ride(box, date="2023-01-01", key=b"\x01", duration=40.0)
    _ride(box, date="2023-01-01", key=b"\x02", duration=3600.0,
          duration_unit="s")
    _ride(box, date="2024-01-01", key=b"\x03", duration=30.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"
    assert comparison.left.units_seen == ()
    because = comparison.comparable["because"]
    assert "which the layer below did not name" in because
    assert "min" not in because.replace("more than one unit", "")
    assert "which the layer below did not name" in windows.render(comparison)


def test_a_source_exclusion_reaches_the_workout_series_too(box):
    """The metric path and the workout path are two calls, so an exclusion
    that travels on one is no evidence about the other."""
    _ride(box, date="2023-01-01", key=b"\x01", source="Watch")
    _ride(box, date="2023-01-02", key=b"\x02", source="Phone")
    _ride(box, date="2024-01-01", key=b"\x03", source="Phone")
    ranges = dict(left=("2023-01-01", "2023-01-31"),
                  right=("2024-01-01", "2024-01-31"))
    both = windows.compare(box, spec=windows.SeriesSpec(activity="Cycling"),
                           **ranges)
    without_phone = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling",
                                     source_exclude=("Phone",)), **ranges)
    assert both.left.n == 2 and both.right.n == 1
    assert without_phone.left.n == 1 and without_phone.right.n == 0
    because = without_phone.comparable["because"]
    assert "Phone" in because and "excluded" in because


def test_an_indoor_filter_is_named_when_a_side_comes_back_empty(box):
    """A side a filter emptied must say which filter, or the reader reads the
    index-wide count as the whole story."""
    _ride(box, date="2023-01-01", key=b"\x01", indoor=1)
    _ride(box, date="2023-01-02", key=b"\x02", indoor=1)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", indoor=False),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "insufficient_data"
    because = comparison.comparable["because"]
    assert "marked indoor" in because
    assert "marked indoor" in windows.render(comparison)


def test_an_explicit_date_anchor_is_named_once_not_twice(box):
    """An explicit date is its own label, so "on {date}" after it prints the
    date twice in both the label and the note."""
    _daily(box, date="2023-02-17", value=70.0)
    comparison = windows.as_comparison(windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, date="2023-02-18"),
        before_days=1, after_days=1))
    assert comparison.label.count("2023-02-18") == 1
    assert comparison.note.count("2023-02-18") == 1
    assert "2023-02-18 on 2023-02-18" not in windows.render(comparison)


def test_a_named_anchor_still_carries_both_its_name_and_its_date(box):
    _fact(box, name="ACL reconstruction", date="2023-09-15")
    _daily(box, date="2023-09-14", value=70.0)
    comparison = windows.as_comparison(windows.window(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        anchor=windows.resolve_anchor(box, fact="ACL"),
        before_days=1, after_days=1))
    assert "ACL reconstruction" in comparison.label
    assert "2023-09-15" in comparison.label
    assert "ACL reconstruction" in comparison.note


def test_the_facts_about_the_figure_are_stated_once_not_once_per_side(box):
    """Said per side, a refusal over a filtered workout figure ran past 600
    characters, and the side lines said it all again. A refusal nobody reads
    is a refusal that failed."""
    _ride(box, date="2023-01-01", key=b"\x01", source="Phone", indoor=1)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=CADENCE_AVG,
                                     source_exclude=("Phone",), indoor=False),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    because = comparison.comparable["because"]
    for once in ("no workout carries", "marked indoor", "excluded",
                 "of Cycling"):
        assert because.count(once) == 1, (once, because)
    # 602 characters when every clause was repeated per side. The cap is
    # loose on purpose: it is here to catch the repetition coming back, not
    # to police four distinct facts, one of which is a 46-character measure
    # identifier the caller chose.
    assert len(because) < 500, len(because)
    rendered = windows.render(comparison)
    assert rendered.count("marked indoor") == 1


def test_a_metric_figures_facts_are_also_stated_once(box):
    _daily(box, date="2023-01-01", value=60.0, source="Phone")
    _daily(box, date="2024-01-01", value=61.0, source="Phone")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr",
                                     source_exclude=("Phone",)),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    because = comparison.comparable["because"]
    assert because.count("excluded") == 1
    # Two sides emptied by the same figure-wide absence are one sentence.
    assert because.count("in that range") == 1
    assert "neither side" in because
    assert len(because) < 220, len(because)


def _metadata(conn, *, workout_id, key, value, unit=None):
    conn.execute(
        "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
        "VALUES(?, 'metadata', ?, ?, ?)", (workout_id, key, value, unit))
    conn.commit()


def test_a_measure_held_as_metadata_is_not_reported_as_held_by_nothing(box):
    """The worst form of the absence defect: not a silence read as emptiness
    but a false absence, printed beside the number that disproves it. A
    workout measure can be held with kind 'metadata' (HKElevationAscended,
    HKAverageMETs, HKWeatherTemperature in real exports), and the CLI's own
    measure table advertises those keys, so this is reachable by following
    the tool's own guidance."""
    _ride(box, date="2023-01-01", key=b"\x01")
    _metadata(box, workout_id=1, key="HKElevationAscended", value=120.0)
    _ride(box, date="2024-01-01", key=b"\x02")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling",
                                     measure="HKElevationAscended"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.left.median == 120.0
    because = comparison.comparable["because"]
    rendered = windows.render(comparison)
    assert "no workout carries" not in because
    assert "no workout carries" not in rendered
    assert "HKElevationAscended is carried by 1 workout" in because
    assert comparison.comparable["verdict"] == "missing_measure_on_one_side"


def test_the_same_key_under_both_kinds_is_counted_once_across_both(box):
    """`workout_measures` groups by (kind, key) and the table's unique index
    is on (workout_id, kind, key), so one key can be held under both kinds.
    Taking the first row as the whole answer would under-count it."""
    _ride(box, date="2023-01-01", key=b"\x01")
    _metadata(box, workout_id=1, key="HKAverageMETs", value=8.0)
    _ride(box, date="2023-01-02", key=b"\x02")
    box.execute(
        "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
        "VALUES(2, 'statistic', 'HKAverageMETs', 9.0, NULL)")
    box.commit()
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling",
                                     measure="HKAverageMETs"),
        left=("2024-01-01", "2024-01-31"), right=("2024-02-01", "2024-02-28"))
    assert "HKAverageMETs is carried by 2 workouts" in \
        comparison.comparable["because"]


def test_an_unlabelled_value_is_not_adopted_into_the_other_periods_unit(box):
    """Two periods with no unit and two in minutes are not four periods in
    minutes. An absent unit is a unit of its own, which is what `queries`
    decided one layer below."""
    _ride(box, date="2023-01-02", key=b"\x01", duration=45.0,
          duration_unit=None)
    _ride(box, date="2023-01-04", key=b"\x02", duration=45.0,
          duration_unit=None)
    _ride(box, date="2023-01-08", key=b"\x03", duration=45.0)
    _ride(box, date="2023-01-10", key=b"\x04", duration=45.0)
    _ride(box, date="2024-01-01", key=b"\x05", duration=45.0)
    _ride(box, date="2024-01-02", key=b"\x06", duration=45.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.left.unit_mixed
    assert comparison.left.units_seen == ("min", "unlabelled")
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"
    earlier = windows.render(comparison).splitlines()[1]
    assert "median 45 min" not in earlier     # it was relabelled as minutes
    assert "more than one unit (min, unlabelled)" in earlier


def test_a_side_with_no_unit_against_a_side_with_one_is_a_mismatch(box):
    """The false ok this module can produce: "both sides carry values in the
    same unit" where one side has no unit at all."""
    for day, key in (("2023-01-01", b"\x01"), ("2023-01-02", b"\x02")):
        _ride(box, date=day, key=key, duration=45.0, duration_unit=None)
    for day, key in (("2024-01-01", b"\x03"), ("2024-01-02", b"\x04")):
        _ride(box, date=day, key=key, duration=45.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mismatch"
    because = comparison.comparable["because"]
    assert "no unit named" in because and "min" in because
    assert "the same unit" not in because
    # And the side line does not print "unlabelled" as though it were a unit
    # somebody measured in.
    earlier = windows.render(comparison).splitlines()[1]
    assert "median 45 unlabelled" not in earlier
    assert "with no unit named" in earlier


def test_an_unlabelled_metric_row_is_a_unit_of_its_own_too(box):
    """`metric_series` dropped NULL units from its set, so a type whose rows
    are partly unitless looked like one unit and the unlabelled values were
    printed under the labelled ones' name."""
    _daily(box, date="2023-01-01", value=60.0, unit="count/min")
    _daily(box, date="2023-01-02", value=61.0, unit=None)
    _daily(box, date="2024-01-01", value=62.0, unit="count/min")
    _daily(box, date="2024-01-02", value=63.0, unit="count/min")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.left.units_seen == ("count/min", "unlabelled")
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"


def test_the_word_for_an_absent_unit_is_the_one_the_layer_below_uses(box):
    """Three layers disagreeing about what an absent unit means is how this
    class of defect came back twice. The word is pinned here."""
    assert windows.UNLABELLED == queries._UNLABELLED


def test_the_thin_gate_counts_the_periods_that_carry_a_value(box):
    """Five periods a side with one measured value each is not a range
    against a range. Empty periods padded the count past the gate the
    constant exists to close, and `no_overlap` was printed over two points."""
    for i, day in enumerate(("2023-01-01", "2023-01-02", "2023-01-03",
                             "2023-01-04", "2023-01-05")):
        _ride(box, date=day, key=bytes([i + 1]),
              hr_avg=140.0 if day == "2023-01-01" else None)
    for i, day in enumerate(("2023-02-01", "2023-02-02", "2023-02-03",
                             "2023-02-04", "2023-02-05")):
        _ride(box, date=day, key=bytes([i + 11]),
              hr_avg=90.0 if day == "2023-02-01" else None)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2023-02-01", "2023-02-28"))
    assert comparison.comparable["verdict"] == "thin_on_one_side"
    assert comparison.comparable["left_n"] == 5
    assert comparison.comparable["left_value_periods"] == 1
    assert "carrying a value" in comparison.comparable["because"]
    assert "no_overlap" not in windows.render(comparison)


def test_a_median_says_how_many_periods_it_rests_on(box):
    """A median resting on one of five periods read as resting on five. The
    partial absence had no representation in the rendered text, which is
    what the model is handed and what the reader is shown."""
    for i, day in enumerate(("2023-01-01", "2023-01-02", "2023-01-03")):
        _ride(box, date=day, key=bytes([i + 1]),
              hr_avg=140.0 if i < 2 else None)
    for i, day in enumerate(("2023-02-01", "2023-02-02", "2023-02-03")):
        _ride(box, date=day, key=bytes([i + 11]), hr_avg=141.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
        left=("2023-01-01", "2023-01-31"), right=("2023-02-01", "2023-02-28"))
    earlier, later = windows.render(comparison).splitlines()[1:3]
    assert "of which 2 periods carry this figure" in earlier
    assert "3 periods" in later and "of which" not in later


def test_a_metric_window_with_an_indoor_flag_is_refused_not_quietly_dropped(box):
    """An argument that cannot apply must not come back as an answer that
    reads as filtered."""
    _daily(box, date="2023-01-01", value=60.0)
    with pytest.raises(ValueError) as caught:
        windows.compare(
            box, spec=windows.SeriesSpec(metric="resting-hr", indoor=True),
            left=("2023-01-01", "2023-01-31"),
            right=("2024-01-01", "2024-01-31"))
    assert "workout figure" in str(caught.value)


def test_the_category_refusal_names_a_tool_the_model_can_call(box):
    _sleep(box, date="2023-01-01", category="Core", seconds=900.0)
    with pytest.raises(ValueError) as caught:
        windows.compare(box, spec=windows.SeriesSpec(metric="sleep"),
                        left=("2023-01-01", "2023-01-31"),
                        right=("2024-01-01", "2024-01-31"))
    assert "query_healthkit" in str(caught.value)
    assert "queries." not in str(caught.value)


def test_a_side_that_carries_values_always_carries_a_unit_name(box):
    """The invariant the cross-side unit test rests on. `unit` is None only
    when the side has no values or when its units are mixed, both of which
    are answered before that test, so by then any difference between the two
    units is a real one. A change that lets a valued side come back with no
    unit at all would put the false ok back, so the invariant is pinned
    rather than left to be re-derived."""
    _daily(box, date="2023-01-01", value=60.0, unit="count/min")
    _daily(box, date="2023-01-02", value=61.0, unit=None)
    _ride(box, date="2023-03-01", key=b"\x01", duration_unit=None)
    _ride(box, date="2023-03-02", key=b"\x02", hr_avg=None)
    _ride(box, date="2023-03-03", key=b"\x03")
    specs = (windows.SeriesSpec(metric="resting-hr"),
             windows.SeriesSpec(activity="Cycling"),
             windows.SeriesSpec(activity="Cycling", measure=HR_AVG),
             windows.SeriesSpec(activity="Cycling", derive_name="pace"))
    for spec in specs:
        for start, end in (("2023-01-01", "2023-01-31"),
                           ("2023-03-01", "2023-03-31"),
                           ("2025-01-01", "2025-01-31")):
            comparison = windows.compare(box, spec=spec, left=(start, end),
                                         right=(start, end))
            for side in (comparison.left, comparison.right):
                if side.value_periods and not side.unit_mixed:
                    assert side.unit is not None, (spec, start, side)


# --------------------------------------------------------------------------- #
# Sample depth, per side
# --------------------------------------------------------------------------- #
#
# §6 of the design doc names four comparability checks and this was the one
# recorded as not implemented: "the median in-interval sample count per side".
# It is what makes T3's "the earlier year's rides carry almost no heart-rate
# samples" a number rather than a remembered caveat, and without it two sides
# whose statistics rest on three readings and on nine hundred read identically.
# The verdict does not use it: how few samples is too few is a judgment, and
# this layer states numbers.

def _ride_at(conn, *, date, epoch, key, hr_avg=140.0, duration=30.0,
             samples=0, first_sample_key=100):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, source_name, start_date, "
        "start_epoch, local_date, dedup_key) VALUES("
        "'HKWorkoutActivityTypeCycling', ?, 'min', 10.0, 'mi', 'Watch', ?, "
        "?, ?, ?)", (duration, f"{date}T09:00:00-05:00", epoch, date, key))
    conn.execute(
        "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
        "VALUES(?, 'statistic', ?, ?, 'count/min')",
        (cur.lastrowid, HR_AVG, hr_avg))
    for i in range(samples):
        conn.execute(
            "INSERT INTO record(type, value_num, unit, start_date, "
            "start_epoch, local_date, dedup_key) VALUES(?, 60, 'count/min', "
            "?, ?, ?, ?)",
            (HR, f"{date}T09:00:00-05:00", epoch + 60.0 * (i + 1), date,
             bytes([first_sample_key + i])))
    conn.commit()


_DEPTH_SPEC = dict(activity="Cycling", measure=HR_AVG)
_DEPTH_LEFT = ("2023-01-01", "2023-01-31")
_DEPTH_RIGHT = ("2023-03-01", "2023-03-31")


def _depth_box(box):
    """Two thin rides on the left, two well-sampled ones on the right."""
    base = 1672585200.0                                   # 2023-01-01 09:00
    day = 86400.0
    _ride_at(box, date="2023-01-05", epoch=base, key=b"\x01", samples=2,
             first_sample_key=10)
    _ride_at(box, date="2023-01-12", epoch=base + 7 * day, key=b"\x02",
             samples=4, first_sample_key=20)
    later = 1677672000.0                                  # 2023-03-01 09:00
    _ride_at(box, date="2023-03-05", epoch=later, key=b"\x03", samples=20,
             first_sample_key=30)
    _ride_at(box, date="2023-03-12", epoch=later + 7 * day, key=b"\x04",
             samples=24, first_sample_key=60)
    return box


def test_each_side_reports_the_median_samples_inside_its_workouts(box):
    comparison = windows.compare(
        _depth_box(box), spec=windows.SeriesSpec(**_DEPTH_SPEC),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week",
        sample_counts=True)
    left, right = comparison.left.sample_depth, comparison.right.sample_depth
    assert left[HR]["median"] == 3.0                      # over 2 and 4
    assert right[HR]["median"] == 22.0                    # over 20 and 24
    assert left[HR]["workouts_counted"] == 2
    assert comparison.comparable["left_sample_depth"] == left
    assert comparison.comparable["right_sample_depth"] == right


def test_the_side_median_is_over_workouts_not_over_periods(box):
    """A median of per-period medians is a different number, and on an
    uneven split it is the wrong one."""
    base = 1672585200.0
    _ride_at(box, date="2023-01-05", epoch=base, key=b"\x01", samples=1,
             first_sample_key=10)
    _ride_at(box, date="2023-01-06", epoch=base + 3600.0 * 24, key=b"\x02",
             samples=1, first_sample_key=20)
    _ride_at(box, date="2023-01-20", epoch=base + 86400.0 * 15, key=b"\x03",
             samples=9, first_sample_key=30)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(**_DEPTH_SPEC),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week",
        sample_counts=True)
    # Three workouts: 1, 1, 9. Over workouts the median is 1; over the two
    # periods' medians (1 and 9) it would be 5.
    assert comparison.left.sample_depth[HR]["median"] == 1.0
    assert comparison.left.sample_depth[HR]["counts"] == (1, 1, 9)


def test_a_comparison_not_asked_for_depth_carries_none_of_it(box):
    comparison = windows.compare(
        _depth_box(box), spec=windows.SeriesSpec(**_DEPTH_SPEC),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week")
    assert comparison.left.sample_depth is None
    assert "left_sample_depth" not in comparison.comparable
    assert "inside each workout" not in windows.render(comparison)


def test_a_metric_side_has_no_sample_depth_to_report(box):
    """A metric period already carries its record count; there is no workout
    interval to count inside."""
    for day in ("2023-01-02", "2023-01-09"):
        _daily(box, date=day, value=60.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week",
        sample_counts=True)
    assert comparison.left.sample_depth is None
    assert "left_sample_depth" not in comparison.comparable


def test_the_rendered_text_states_both_depths_and_reads_as_two_numbers(box):
    comparison = windows.compare(
        _depth_box(box), spec=windows.SeriesSpec(**_DEPTH_SPEC),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week",
        sample_counts=True)
    text = windows.render(comparison)
    # The record type is named in the words the metric registry uses, not by
    # its HealthKit identifier.
    assert "median of 3 heart rate records inside each workout" in text
    assert "median of 22 heart rate records inside each workout" in text
    lowered = text.lower()
    for word in FORBIDDEN:
        assert word not in lowered, word


def test_an_anchored_window_asked_for_depth_carries_it_too(box):
    base = 1672585200.0
    _ride_at(box, date="2023-01-05", epoch=base, key=b"\x01", samples=2,
             first_sample_key=10)
    _ride_at(box, date="2023-01-20", epoch=base + 86400.0 * 15, key=b"\x02",
             samples=8, first_sample_key=20)
    comparison = windows.as_comparison(
        windows.window(box, spec=windows.SeriesSpec(**_DEPTH_SPEC),
                       anchor=windows.resolve_anchor(box, date="2023-01-13"),
                       before_days=10, after_days=10, period="week",
                       sample_counts=True),
        sample_counts=True)
    assert comparison.left.sample_depth[HR]["median"] == 2.0
    assert comparison.right.sample_depth[HR]["median"] == 8.0


def test_a_workout_with_no_window_to_count_in_is_not_a_depth_of_zero(box):
    base = 1672585200.0
    _ride_at(box, date="2023-01-05", epoch=base, key=b"\x01", samples=4,
             first_sample_key=10)
    _ride_at(box, date="2023-01-12", epoch=base + 86400.0 * 7, key=b"\x02",
             samples=0, duration=None)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(**_DEPTH_SPEC),
        left=_DEPTH_LEFT, right=_DEPTH_RIGHT, period="week",
        sample_counts=True)
    depth = comparison.left.sample_depth[HR]
    assert depth["median"] == 4.0
    assert depth["workouts_counted"] == 1
    assert depth["workouts_without_a_count"] == 1
    assert "inside 1 of its 2 workouts" in windows.render(comparison)


def test_the_depth_clause_adds_no_phrase_an_eval_scorer_reads(box):
    """The standing rule, applied before the phrase ships rather than after.

    No wording a tool produces may be what a scorer looks for, in either
    direction: forbidding such a phrase flags faithful answers and requiring
    one admits unfaithful ones. The rendered text around this clause already
    matches the gap family ("4 periods with no data"), which is recorded in
    ROADMAP #16, so the clause is scanned on its own.
    """
    import re
    import sys

    sys.path.insert(0, str(_TESTS_DIR))
    from run_agent_eval import (DECLINE_PATTERN, DIAGNOSTIC_PATTERNS,
                                DIRECTIVE_ADVICE, GAP_PATTERNS)

    shapes = [
        {HR: {"median": 3.0, "counts": (2, 4), "workouts_counted": 2,
              "workouts_without_a_count": 0, "reasons": ()}},
        {HR: {"median": 4.0, "counts": (4,), "workouts_counted": 1,
              "workouts_without_a_count": 1,
              "reasons": ("workout has no duration",)}},
        {HR: {"median": None, "counts": (), "workouts_counted": 0,
              "workouts_without_a_count": 2,
              "reasons": ("workout has no duration",) * 2}},
    ]
    families = {"no_diag": DIAGNOSTIC_PATTERNS, "decline": [DECLINE_PATTERN],
                "gap": GAP_PATTERNS,
                "directive": [re.escape(p) for p in DIRECTIVE_ADVICE]}
    for shape in shapes:
        clause = windows._depth_clause(
            windows.SideSummary(
                n=1, empty_periods=0, min=1.0, max=1.0, median=1.0, mean=1.0,
                unit="count/min", first="2023-01-01", last="2023-01-07",
                sample_depth=shape))
        assert clause
        for family, patterns in families.items():
            for pattern in patterns:
                assert not re.search(pattern, clause, re.IGNORECASE), \
                    f"{family}: {pattern} matched {clause!r}"
