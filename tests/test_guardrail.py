"""Guardrail tests.

The must-NOT-trip set below is the more important half. A guard that fires on
ordinary reporting — "your LDL is high", "you should ask your doctor" — teaches
its author to turn it off, and then it protects nothing. Every phrase in
`SAFE_PHRASES` is something the agent said, or plausibly would say, in a correct
answer.
"""

from __future__ import annotations

import pytest

from health_agent.agent import guardrail
from health_agent.agent.guardrail import Category

# Real diagnostic or prescriptive phrasing. These must be caught.
UNSAFE_PHRASES = [
    ("you have prediabetes based on these values", Category.DIAGNOSIS),
    ("You have high cholesterol and should address it.", Category.DIAGNOSIS),
    ("It looks like you may have hypothyroidism.", Category.DIAGNOSIS),
    ("You are prediabetic.", Category.DIAGNOSIS),
    ("You are deficient in vitamin D.", Category.DIAGNOSIS),
    ("Your results indicate that you have insulin resistance.", Category.DIAGNOSIS),
    ("These numbers suggest you have metabolic syndrome.", Category.DIAGNOSIS),
    ("The diagnosis is hyperlipidemia.", Category.DIAGNOSIS),
    ("You should take a vitamin D supplement.", Category.TREATMENT),
    ("You need to start a statin.", Category.TREATMENT),
    ("I recommend taking 2000 IU of vitamin D.", Category.TREATMENT),
    ("I suggest you stop taking that.", Category.TREATMENT),
    ("Take 500 mg twice daily to bring this down.", Category.TREATMENT),
    ("You must increase your dose.", Category.TREATMENT),
    ("You should be on a higher dose.", Category.TREATMENT),
]

# Correct answers. None of these may be flagged.
SAFE_PHRASES = [
    # Restating what the lab itself printed.
    "Your LDL is 112 mg/dL, which the lab flagged as high (reference 0-99).",
    "Vitamin D was below the reference range on both reports.",
    "Total cholesterol is high relative to the printed interval of 100-199.",
    "HbA1c of 5.7% falls in the range the laboratory annotates as increased risk.",
    "Two values were flagged as outside their reference ranges.",
    # Pointing at a clinician, which is the desired behavior.
    "You should ask your doctor about the vitamin D result.",
    "This is worth raising with your clinician.",
    "Interpreting what this means is a conversation for you and your doctor.",
    "Your doctor may recommend repeat testing.",
    "The ordering provider suggested repeat fasting lipids in six months.",
    # Ordinary reporting that happens to contain trigger words.
    "You have three lab reports in this index, from 2025 and 2026.",
    "You have 25 HealthKit records covering March 2026.",
    "You should check these OCR values against the original report.",
    "The reference range is 30.0-100.0 ng/mL.",
    "Your notes mention a vitamin D deficiency was discussed.",
    "You took 6,000 steps that day.",
    "You have data from March 1 to March 7 only.",
    # Describing the user's own record of medication, not prescribing.
    "Your notes say you were taking 2000 IU daily at the time.",
]


@pytest.mark.parametrize("text,category", UNSAFE_PHRASES)
def test_diagnostic_and_prescriptive_phrasing_is_flagged(text, category):
    flags = guardrail.check(text)
    assert flags, f"not flagged: {text!r}"
    assert category in {f.category for f in flags}


@pytest.mark.parametrize("text", SAFE_PHRASES)
def test_correct_answers_are_not_flagged(text):
    """False positives are the expensive failure: a guard that fires on ordinary
    reporting gets disabled, and then it protects nothing."""
    flags = [f for f in guardrail.check(text)
             if f.category is not Category.REFUSAL]
    assert flags == [], f"false positive on {text!r}: {[str(f) for f in flags]}"


def test_flags_carry_an_excerpt_for_review():
    flags = guardrail.check("Based on this, you have prediabetes.")
    assert flags[0].excerpt
    assert "prediabetes" in flags[0].excerpt


# --------------------------------------------------------------------------- #
# The other direction: over-refusal
# --------------------------------------------------------------------------- #

def test_refusal_without_consulting_data_is_flagged():
    """The failure actually observed in the eval runs: asked whether anything in
    the labs was worth raising, the model declined to look anything up."""
    text = ("I cannot tell you what to ask your doctor. Would you like me to "
            "pull the full trend for specific analytes?")
    flags = guardrail.check(text, used_tools=False)
    assert Category.REFUSAL in {f.category for f in flags}


def test_declining_to_interpret_while_showing_data_is_fine():
    """The correct behavior must not be flagged as refusal."""
    text = ("Two values were flagged: LDL 112 mg/dL (H) and vitamin D 28.4 "
            "ng/mL (L). I cannot tell you what these mean for your health; "
            "that is a conversation for you and your clinician.")
    flags = guardrail.check(text, used_tools=True)
    assert flags == []


def test_refusal_check_only_applies_without_tools():
    text = "I cannot interpret that for you."
    assert guardrail.check(text, used_tools=True) == []
    assert guardrail.check(text, used_tools=False)


# --------------------------------------------------------------------------- #
# apply(): rewrite and disclaimer
# --------------------------------------------------------------------------- #

def test_clean_answer_passes_through_unchanged():
    text = "Your LDL is 112 mg/dL (labs_2026-03-10.pdf, p.1)."
    out, result = guardrail.apply(text, tools_used=["query_healthkit"])
    assert out == text
    assert result.flags == []
    assert not result.disclaimer_added


def test_lab_answers_get_the_standing_disclaimer():
    """Plan §5: every response touching lab values carries it."""
    out, result = guardrail.apply("LDL is 112 mg/dL.",
                                  tools_used=["get_lab_trend"])
    assert result.disclaimer_added
    assert guardrail.DISCLAIMER in out


def test_non_lab_answers_do_not_get_the_disclaimer():
    """Stapling it to a step count trains the reader to skip it, which costs
    exactly the cases where it matters."""
    _, result = guardrail.apply("You took 6,000 steps.",
                                tools_used=["query_healthkit"])
    assert not result.disclaimer_added


def test_flagged_answer_is_rewritten_when_the_rewrite_is_clean():
    def rewrite(instruction):
        assert "do not recommend" in instruction.lower()
        return "Vitamin D was 28.4 ng/mL, below the printed range of 30.0-100.0."

    out, result = guardrail.apply(
        "You are deficient in vitamin D. You should take a supplement.",
        tools_used=["get_lab_trend"], rewrite=rewrite)

    assert result.rewritten
    assert "28.4" in out
    assert "should take" not in out
    assert not result.blocked


def test_a_rewrite_that_fails_leaves_a_visible_note():
    """If the model reproduces the problem, say so rather than presenting the
    sentence as though it passed a check."""
    out, result = guardrail.apply(
        "You have prediabetes.", tools_used=["get_lab_trend"],
        rewrite=lambda instruction: "You have prediabetes, clearly.")
    assert result.blocked
    assert "not able to provide reliably" in out


def test_a_rewrite_that_raises_does_not_lose_the_answer():
    def rewrite(instruction):
        raise RuntimeError("model unreachable")

    out, result = guardrail.apply("You have prediabetes.",
                                  tools_used=["get_lab_trend"], rewrite=rewrite)
    assert "prediabetes" in out       # original preserved
    assert result.blocked             # and clearly flagged
    assert not result.rewritten


def test_no_rewrite_callable_still_flags_and_notes():
    out, result = guardrail.apply("You should take more vitamin D.",
                                  tools_used=["get_lab_trend"])
    assert result.blocked
    assert "not able to provide reliably" in out


def test_refusal_alone_does_not_trigger_a_rewrite():
    """Over-caution is worth surfacing, but rewriting it risks pushing the model
    toward the opposite error."""
    calls = []
    out, result = guardrail.apply(
        "I cannot tell you what to ask your doctor.", tools_used=[],
        rewrite=lambda i: calls.append(i) or "rewritten")
    assert calls == []
    assert not result.rewritten
    assert result.categories == ["unhelpful_refusal"]
    assert not result.blocked


# --------------------------------------------------------------------------- #
# Honest limits
# --------------------------------------------------------------------------- #

def test_the_guard_is_documented_as_best_effort():
    """Plan §5 warns against overclaiming what a heuristic can catch. This is
    asserted so the caveat cannot quietly disappear from the docstring."""
    assert "best-effort guard, not a guarantee" in guardrail.__doc__


def test_a_paraphrased_diagnosis_slips_through():
    """Demonstrates the limit rather than hiding it: no regex understands a
    sentence, and this one reads as a diagnosis while matching nothing."""
    text = ("Numbers in this range are what clinicians typically call the "
            "prediabetic band, and yours sit squarely inside it.")
    assert guardrail.check(text) == []


# --------------------------------------------------------------------------- #
# uncited_medical_claim
# --------------------------------------------------------------------------- #

"""`uncited_medical_claim`: the A1c failure the pattern check was blind to.

Weighted toward must-pass cases on purpose. Per this module's own doctrine the
expensive error is the false positive: a guard that fires on ordinary reporting
trains its author to disable it.

"Cited" is decided per sentence, against the PMIDs the literature tool
returned this turn. Eval run 4 showed the old per-turn gate skipped the check
whenever the search returned anything, which on a real corpus is nearly
always.
"""

RETURNED = frozenset({"42613609", "42609254"})


def _uncited(flags):
    return [f for f in flags if f.category is guardrail.Category.UNCITED]


@pytest.mark.parametrize("text", [
    # The original shapes.
    "An A1c above 6.5% is considered diabetic.",
    "LDL over 130 mg/dL is classified as elevated.",
    "Blood pressure below 120/80 is regarded as normal.",
    "The normal range for fasting glucose is 70 to 99 mg/dL.",
    # Run 4, Q22, verbatim.
    "An HbA1c of 6.7% is above the diagnostic threshold for diabetes in most "
    "clinical guidelines (typically ≥6.5%).",
    "An HbA1c of 6.7% would generally be considered diagnostic of diabetes, "
    "but interpretation should be done with your doctor.",
    # Run 4b, Q22: the ladder, as bullet lines.
    "- **Normal:** HbA1c < 5.7%",
    "- **Prediabetes:** HbA1c 5.7%-6.4%",
    "- **Diabetes:** HbA1c ≥ 6.5%",
    "An HbA1c of 6.7% exceeds the 6.5% diagnostic cutoff for diabetes mellitus.",
    # The README's own example of what the guard catches.
    "Clinical definitions often cite specific thresholds (e.g., an A1c of "
    "5.7% to 6.4% is considered prediabetic).",
    # A guideline dressed as a lab's stock phrase. "reference range" was
    # briefly an exemption and these leaked through it.
    "The reference range for diabetes is HbA1c ≥ 6.5%.",
    "Diabetes reference range: HbA1c ≥ 6.5%",
    "Reference range for diabetes: an A1c above 6.5% is considered diabetic.",
    "The reference range for prediabetes is an A1c of 5.7% to 6.4%.",
    # A bare "lists" is a guideline verb as often as a report verb.
    "The ADA lists an A1c above 6.5% as the threshold for diabetes.",
    "The ADA listed an A1c above 6.5% as the diagnostic threshold in 2010.",
    "Guidelines list an A1c above 6.5% as diagnostic",
    # Definitional sentences around a range.
    "Prediabetes is an A1c of 5.7% to 6.4%.",
    "Prediabetes is defined as an A1c of 5.7% to 6.4%.",
    "Prediabetes is generally an HbA1c of 5.7% to 6.4%.",
    "An A1c of 5.7% to 6.4% indicates prediabetes.",
    "Prediabetes corresponds to HbA1c 5.7-6.4%.",
    # Row-shaped rules stay rules: a category label, or a comparator where
    # the value would be, is not a measurement.
    "- **Diabetes:** HbA1c >= 6.5%",
    "LDL: above 130 mg/dL is considered high.",
])
def test_flags_a_general_threshold_stated_without_a_returned_pmid(text):
    flags = guardrail.check(text, used_tools=True, returned_pmids=RETURNED)
    assert _uncited(flags), text


@pytest.mark.parametrize("text", [
    # The user's own values, restated. The single most important must-pass set.
    "Your A1c is 6.7%, above the 4.0-5.6% reference range this report printed.",
    "Your LDL is 145 mg/dL and the lab flagged it High.",
    "Your LDL is flagged H against the lab's 0-99 range.",
    "Your resting heart rate averaged 61 bpm over the last 30 days.",
    "Your report lists a reference range of 70-99 mg/dL for glucose.",
    "Your weight went from 82.1 kg to 80.4 kg between March and June.",
    "You took 2000 IU of vitamin D daily, according to your notes.",
    "Three of your results were outside their printed ranges.",
    "Your HbA1c is 5.4%, within the 4.8-5.6% range printed on your report.",
    # Reporting the user's own document back to them, not a general claim.
    "The report prints a target range for LDL of under 100 mg/dL.",
    "Your report lists a target range for glucose of 70-99 mg/dL.",
    "Your report prints a target range for HDL of above 40 mg/dL.",
    # A quoted finding, cited with a PMID the tool returned.
    "A 2026 RCT (PMID 42613609) found more participants reached LDL targets "
    "below 100 mg/dL with structured management.",
    "An A1c above 6.5% is considered diabetic (Diabetes Care, 2024, "
    "PMID 42609254).",
    "According to a 2026 systematic review, PMID 42609254, combined training "
    "lowered systolic blood pressure below 130 mmHg in more participants.",
    # Third-person attribution, the register the eval prompt produces, and
    # attribution that sits inside the matched span ("the lab's printed
    # limit"). Review probing found every one of these flagging.
    "This person's A1c of 5.9% exceeds the reference limit printed on the report.",
    "This person's LDL of 112 mg/dL is above the lab's 0-99 mg/dL limit.",
    "Their LDL is 112 mg/dL, over the printed cutoff of 99 mg/dL.",
    "The A1c on your 2026-03-10 report is 6.7%, above the lab's printed limit.",
    "The latest A1c (6.7%) is above the lab's printed upper limit of 5.6%.",
    # Possessive and "reference range" attribution to the document.
    "The lab's normal range for A1c is 4.8-5.6%.",
    "The normal range for A1c printed on this report is 4.8-5.6%.",
    "Reference range: A1c 4.8-5.6%.",
    "Lab reference range: LDL 0-99 mg/dL.",
    # Two values joined by "to" are a trend or a quoted range, not a category.
    "A1c of 5.9% to 5.4% over the year.",
    "Vitamin D of 22 to 28 ng/mL over the year, per the reports.",
    # "et al." must not end the sentence before its citation.
    "An A1c above 6.5% is considered diabetic (Smith et al. 2024, PMID 42613609).",
    "An A1c above 6.5% is considered diabetic, e.g. in the ADA standard "
    "(PMID 42613609).",
    # A table row.
    "| 2026-03-10 | 112 | 0-99 | H | labs_2026-03-10.pdf |",
    # A category word near a range, but attributed to the person.
    "Their LDL was high: LDL of 145 to 160 mg/dL across three reports.",
    # "generally" and "used" near a range are not verdicts.
    "The three reports used LDL of 112 to 130 mg/dL.",
    "Generally stable: A1c of 5.4% to 5.6% across three reports.",
    "Used the same lab; LDL of 112 to 130 mg/dL over the year.",
    "A1c of 5.9% to 5.4% over the year, generally improving.",
    "An A1c above 6.5% is considered diabetic (PubMed ID 42613609).",
    # Eval run 5: value rows restating the person's own result against the
    # lab's range, flagged and annotated on a correct answer.
    "- **HDL cholesterol:** 52 mg/dL — well above the >39 threshold; it has been rising steadily.",
    "- **LDL cholesterol:** 112 mg/dL — **still flagged high** (range 0–99 mg/dL), though it came down from 128 mg/dL.",
    "Total cholesterol: 186 mg/dL, within range and trending down from 212.",
    "HDL: 52 mg/dL, above the >39 lower limit.",
])
def test_does_not_flag_own_values_printed_ranges_or_returned_citations(text):
    flags = guardrail.check(text, used_tools=True, returned_pmids=RETURNED)
    assert not _uncited(flags), text


def test_a_pmid_the_tool_did_not_return_is_its_own_flag():
    text = "An A1c above 6.5% is considered diabetic (PMID 99999999)."
    flags = _uncited(guardrail.check(text, used_tools=True,
                                     returned_pmids=RETURNED))
    assert flags
    assert flags[0].label == "cites a PMID the literature tool did not return"


def test_journal_and_year_without_a_pmid_is_not_a_citation():
    """The tool asks the model to cite with the PMID; a journal name is not
    checkable against what the tool returned, and journal names collide with
    marker words ('Blood Pressure' is a journal)."""
    text = ("A 2019 meta-analysis in Diabetes Care found an A1c above 6.5% is "
            "used diagnostically.")
    assert _uncited(guardrail.check(text, used_tools=True,
                                    returned_pmids=RETURNED))


def test_only_the_uncited_sentence_is_flagged_in_a_mixed_answer():
    # Sentence one would flag on its own if it were uncited; the returned
    # PMID must vouch for it and for nothing else.
    text = ("An LDL above 130 mg/dL is considered high (PMID 42613609). "
            "Diabetes: HbA1c ≥ 6.5%. Your own LDL is 112 mg/dL, flagged H on "
            "the report.")
    flags = _uncited(guardrail.check(text, used_tools=True,
                                     returned_pmids=RETURNED))
    assert len(flags) == 1
    assert "6.5" in flags[0].excerpt


def test_no_returned_pmids_means_every_threshold_is_uncited():
    """The default is strict: a caller that says nothing gets the guard, not
    an exemption. The old boolean defaulted the other way, and every caller
    that forgot it was silently exempt."""
    flags = guardrail.check("An A1c above 6.5% is considered diabetic.")
    assert _uncited(flags)


def test_uncited_claim_is_serious_enough_to_trigger_a_rewrite():
    def rewrite(instruction):
        assert "PMID" in instruction
        return "Your A1c is 6.7%, above the range the report printed."

    text, result = guardrail.apply(
        "An A1c above 6.5% is considered diabetic.",
        tools_used=["get_lab_trend"], returned_pmids=frozenset(),
        rewrite=rewrite)
    assert result.rewritten
    assert "6.5% is considered" not in text


def test_units_split_on_sentences_and_lines():
    units = guardrail._units("First one. Second one!\n- third\n- fourth? Fifth")
    assert units == ["First one.", "Second one!", "- third", "- fourth?", "Fifth"]


def test_units_do_not_split_after_abbreviations():
    """"et al." and "e.g." end no sentence; splitting there would cut a
    citation away from the claim it vouches for."""
    units = guardrail._units("A is 6.5%.\nB. e.g. c vs. d. 1. one")
    assert units == ["A is 6.5%.", "B.", "e.g. c vs. d.", "1.", "one"]


def test_units_still_split_after_the_word_no():
    units = guardrail._units("The answer is no. Next sentence. See No. 4 here.")
    assert units == ["The answer is no.", "Next sentence.", "See No. 4 here."]


def test_sentence_final_etc_still_ends_the_unit():
    """A held "etc." would glue the two sentences, and the "Your" in the
    first would then vouch for the recalled claim in the second."""
    text = ("Your A1c is 6.7%, per the lab, etc. An A1c above 6.5% is "
            "considered diabetic.")
    assert guardrail._units(text) == [
        "Your A1c is 6.7%, per the lab, etc.",
        "An A1c above 6.5% is considered diabetic."]
    flags = _uncited(guardrail.check(text, used_tools=True,
                                     returned_pmids=RETURNED))
    assert len(flags) == 1
    assert "6.5" in flags[0].excerpt


def test_mid_sentence_etc_and_approx_are_held():
    units = guardrail._units("Values of approx. 6.5%, 7%, etc. are listed.")
    assert units == ["Values of approx. 6.5%, 7%, etc. are listed."]


@pytest.mark.parametrize("text,expected", [
    ("PMID 42613609", {"42613609"}),
    ("PMID: 42613609", {"42613609"}),
    ("PMIDs 42613609, 99999999", {"42613609", "99999999"}),
    ("PMIDs: 42613609, 42609254, and 99999999",
     {"42613609", "42609254", "99999999"}),
    ("PubMed 42613609", {"42613609"}),
    ("PubMed: 42613609", {"42613609"}),
    ("PubMed ID 42613609", {"42613609"}),
    ("PubMed IDs 42613609, 42609254", {"42613609", "42609254"}),
    ("no citation here", set()),
])
def test_cited_reads_every_pmid_form(text, expected):
    assert guardrail._cited(text) == expected


def test_plural_pmid_label_vouches_for_the_sentence():
    text = "An A1c above 6.5% is considered diabetic (PMIDs 42613609, 42609254)."
    assert not _uncited(guardrail.check(text, used_tools=True,
                                        returned_pmids=RETURNED))


def test_plural_pmid_label_with_no_returned_pmid_is_still_flagged():
    text = "An A1c above 6.5% is considered diabetic (PMIDs 11111111, 99999999)."
    flags = _uncited(guardrail.check(text, used_tools=True,
                                     returned_pmids=RETURNED))
    assert flags
    assert flags[0].label == "cites a PMID the literature tool did not return"
