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

from health_agent.agent import guardrail
from health_agent.store import queries, sqlite_schema, windows

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
          distance=10.0, distance_unit="mi", source="Watch", indoor=None,
          activity="HKWorkoutActivityTypeCycling"):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, source_name, start_date, "
        "start_epoch, local_date, indoor, dedup_key) "
        "VALUES(?, ?, 'min', ?, ?, ?, ?, 1772830800.0, ?, ?, ?)",
        (activity, duration, distance, distance_unit, source,
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
    unit belongs to the side that is not mixed."""
    _ride(box, date="2023-01-01", key=b"\x01", distance=10.0,
          distance_unit="mi")
    _ride(box, date="2023-01-02", key=b"\x02", distance=10.0,
          distance_unit="km")
    _ride(box, date="2024-01-01", key=b"\x03", distance=10.0,
          distance_unit="mi")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(activity="Cycling", derive_name="pace"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"),
        period="month")
    assert comparison.comparable["verdict"] == "unit_mixed_on_one_side"
    because = comparison.comparable["because"]
    assert "the earlier side" in because
    assert "min/km" in because and "min/mi" in because
    assert "the later side" not in because
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
