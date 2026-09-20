"""Medical guardrail: the output check that sits behind the system prompt.

Plan §5 asks for two layers. The first is the system prompt, which instructs the
model to describe and contextualize but never diagnose or prescribe. This is the
second: a pattern check over the finished answer that catches phrasing the prompt
did not prevent, and either triggers one rewrite pass or appends a disclaimer.

**This is a best-effort guard, not a guarantee.** A regex cannot understand a
sentence. It will miss diagnoses phrased in ways these patterns don't anticipate,
and it can only ever be a backstop to the prompt, never a replacement for it. The
project should not claim otherwise, and neither should its README.

**The patterns are narrow on purpose, because the expensive error is the false
positive.** "Your LDL is high" is a restatement of the lab's own flag and must
pass; "you have high cholesterol" is a diagnosis and must not. "You should ask
your doctor" must pass; "you should take 2000 IU of vitamin D" must not. A guard
that fires on ordinary reporting trains its author to disable it.

**It checks both directions.** Measured across 40 eval answers, the model
produced zero diagnostic statements — the observed failure was the opposite one:
asked whether anything in the labs was worth raising with a doctor, it declined
to look anything up at all and answered with a question. Over-caution withholds
information that is already the user's, and no diagnosis-hunting regex would ever
catch it. So `unhelpful_refusal` is flagged too, and reported the same way.

**A citation is a property of the sentence, not the turn.** The uncited-claim
check once skipped itself whenever the turn's literature search returned any
finding. Against an eight-article fixture that meant "usually runs"; against
2,000 real abstracts it meant "almost never runs", because nearly every query
returns something, and the first real eval reproduced the exact A1c leak the
check was built for. Now each threshold sentence must carry a PMID the tool
actually returned this turn, or it is flagged; a PMID the tool did not return
is flagged too, so a recalled claim cannot be laundered with an invented one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    DIAGNOSIS = "diagnosis"
    TREATMENT = "treatment"
    REFUSAL = "unhelpful_refusal"
    UNCITED = "uncited_medical_claim"


# Conditions the model might assert. Kept as an explicit list rather than a
# catch-all so "you have three lab reports" cannot trip a diagnosis flag.
_CONDITION = (
    r"(?:pre-?)?diabetes|prediabetic|diabetic|hypertension|hypertensive|"
    r"hyperlipidemia|hypercholesterolemia|high cholesterol|anemia|anaemia|"
    r"anemic|hypothyroidism|hyperthyroidism|thyroid disease|"
    r"metabolic syndrome|insulin resistance|kidney disease|liver disease|"
    r"heart disease|fatty liver|osteoporosis|\w+ deficiency|\w+ insufficiency|"
    r"\w+ syndrome|\w+ disorder"
)

PATTERNS: list[tuple[Category, str, str]] = [
    (Category.DIAGNOSIS, "asserts a condition",
     rf"\byou (?:have|appear to have|likely have|probably have|may have|"
     rf"might have|could have|are showing signs of)\s+"
     rf"(?:a |an |mild |moderate |severe |early |borderline )?(?:{_CONDITION})\b"),
    (Category.DIAGNOSIS, "labels the person",
     r"\byou are (?:pre-?diabetic|diabetic|hypertensive|an?emic|hypothyroid|"
     r"hyperthyroid|obese|deficient in \w+)\b"),
    (Category.DIAGNOSIS, "reads results as proof of a condition",
     rf"\b(?:this|these|that|your (?:results?|numbers?|labs?|values?)) "
     rf"(?:indicates?|suggests?|means?|confirms?|shows?) (?:that )?"
     rf"you (?:have|are)\s+(?:a |an )?(?:{_CONDITION})\b"),
    (Category.DIAGNOSIS, "states a diagnosis",
     r"\b(?:the|your) diagnosis is\b|\bi (?:can |would )?diagnos"),

    (Category.TREATMENT, "directs a medication or supplement change",
     r"\byou (?:should|need to|ought to|must|have to) "
     r"(?:take|start|begin|stop|discontinue|increase|decrease|reduce|"
     r"supplement with|be on)\b"),
    (Category.TREATMENT, "recommends taking something",
     r"\bi (?:recommend|suggest|advise) (?:that )?(?:you )?"
     r"(?:take|taking|start|starting|stop|stopping|increas|decreas|"
     r"supplement|a dose)"),
    # A dose only counts when it is being *directed*. "Your notes say you were
    # taking 2000 IU daily" is reporting the user's own record back to them, and
    # flagging it would fire the guard on ordinary retrieval of their history.
    (Category.TREATMENT, "prescribes a dose",
     r"\b(?:you (?:should|need to|must|ought to|have to)|"
     r"i (?:recommend|suggest|advise))\b[^.]{0,40}"
     r"\b\d+\s?(?:mg|mcg|µg|g|iu|ml)\b"),
    (Category.TREATMENT, "directs a dose",
     r"(?:^|[.!?]\s+|\n)\s*(?:take|start taking|begin taking|use)\s+"
     r"\d+\s?(?:mg|mcg|µg|g|iu|ml)\b"),
]

# Phrases that read as a refusal to help. Only meaningful when the answer also
# used no tools — declining to interpret while presenting data is correct.
REFUSAL_PATTERNS = [
    r"\bi (?:cannot|can't|am not able to|am unable to) (?:tell|say|advise|"
    r"determine|interpret|answer)",
    r"\bi'm not able to\b", r"\bi am not qualified\b",
    r"\bwould you like me to (?:pull|look|check|fetch|retrieve)",
    r"\bwhich (?:analytes?|metrics?|values?) would you like\b",
    r"\bplease (?:specify|clarify) which\b",
]

# General clinical claims stated as fact, judged one sentence at a time.
#
# The distinguishing signal is a threshold attached to a GENERAL subject rather
# than to "your". Adversarial probing of v0.1.0 found the model volunteering an
# A1c threshold from training data, uncited and undated, and nothing in the
# other patterns could see it. This is that hole. Eval run 4 added the shapes
# the model actually uses on a real corpus: the number after "diagnostic
# threshold", symbol comparators, and category ladders ("Diabetes: HbA1c ≥
# 6.5%").
#
# Three things exempt a sentence. `_REPORTING_CONTEXT` matches phrasing that
# attributes the range to a document ("the report prints...", "according
# to..."). `_PERSONAL_CONTEXT` matches "you"/"your" shortly before the
# threshold: "your A1c is 6.7%, above the 4.0-5.6% range this report printed"
# is a restatement, not a claim. And a `PMID <n>` in the sentence, where <n>
# is one the literature tool returned this turn, makes it a report of
# published evidence. Both context checks are lookbacks applied in `check()`,
# since Python's `re` only allows fixed-width lookbehind and these phrases
# vary in length.
_REPORTING_CONTEXT = re.compile(
    r"\b(?:report|reports|lab|labs|document|documents|chart|charts|"
    r"record|records|note|notes|file|files)\b[^.]{0,20}\b(?:prints?|"
    r"printed|lists?|listed|shows?|showed|states?|stated|says?|said|"
    r"flagged?)\b|\baccording to\b",
    re.IGNORECASE,
)

_MARKER = (
    r"a1c|hba1c|ldl|hdl|cholesterol|triglycerides?|glucose|blood pressure|"
    r"systolic|diastolic|bmi|tsh|ferritin|vitamin d|creatinine|egfr|crp"
)

_PERSONAL_CONTEXT = re.compile(r"\byour?\b", re.IGNORECASE)
_PMID = re.compile(r"\bPMID\s*:?\s*(\d{5,9})\b", re.IGNORECASE)
_UNIT_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")

_NUM = r"\d[\d.,]*\s?%?\s?(?:mg/dl|mmol/l|mg/l|bpm|kg/m2|mmhg|%)?"
_CMP_WORD = r"(?:above|below|over|under|greater than|less than|at or above|at or below|exceeds?|meets?)"
_CMP_SYM = r"(?:≥|≤|>=|<=|>|<)"
_JUDGED = r"(?:considered|classified|regarded|defined|diagnostic|generally|used|recognized|recognised)"

UNCITED_PATTERNS: list[tuple[str, str]] = [
    # Units are already single sentences or lines (see `_units`), so the
    # spans below use `[^\n]` rather than `[^.]`: a `.` inside "6.7%" must
    # not end the span.
    ("states a general clinical threshold",
     # marker ... above/below ... number ... is considered/diagnostic
     rf"\b(?:an?|the)?\s*(?:{_MARKER})\b[^\n]{{0,30}}\b{_CMP_WORD}\b"
     rf"[^\n]{{0,25}}\b{_NUM}\b[^\n]{{0,25}}\b(?:is|are|would be|was)\s+"
     rf"(?:generally\s+|widely\s+|typically\s+)?{_JUDGED}\b"),
    ("states a general clinical threshold",
     # marker ... above/exceeds the (6.5% diagnostic) threshold/cutoff
     rf"\b(?:{_MARKER})\b[^\n]{{0,40}}\b{_CMP_WORD}\b[^\n]{{0,25}}"
     rf"\b(?:threshold|cut-?off|criteri(?:on|a)|limit)s?\b"),
    ("states a general clinical threshold",
     # marker with a symbol comparator: "HbA1c ≥ 6.5%", "LDL > 130 mg/dL"
     rf"\b(?:{_MARKER})\b\s*(?:of\s*)?{_CMP_SYM}\s*{_NUM}"),
    ("states a general clinical threshold",
     # a range presented as a category: "HbA1c 5.7%-6.4%",
     # "an A1c of 5.7% to 6.4% is considered prediabetic"
     rf"\b(?:{_MARKER})\b\s*(?:of\s*)?{_NUM}\s*(?:to|-|–|—)\s*{_NUM}"),
    ("states a general clinical threshold",
     # "considered diagnostic of <condition>"
     rf"\b(?:{_MARKER})\b[^\n]{{0,60}}\bconsidered\s+diagnostic\s+(?:of|for)\b"),
    ("states a normal range as general fact",
     rf"\b(?:the\s+)?(?:normal|healthy|optimal|typical|target)\s+"
     rf"(?:range|level|value)s?\s+(?:for|of)\s+(?:\w+\s+)?(?:{_MARKER})\b"),
]

UNCITED_REWRITE = (
    "Your previous answer stated a general medical fact without a citation "
    "the literature tool returned this turn. For each such statement, either "
    "attach the PMID of the tool finding it comes from, or remove it and say "
    "that your literature corpus does not cover it. Keep this person's own "
    "values and the reference ranges their reports printed."
)

DISCLAIMER = (
    "These are the values recorded in your own files, not medical advice. "
    "What they mean for you is a conversation for you and your clinician."
)

REWRITE_INSTRUCTION = (
    "Your previous answer contained {what}. Restate it using only what the tool "
    "results showed: the values, the reference ranges and flags the reports "
    "printed, how things changed, and what the user's own notes say. Do not "
    "name or imply a condition, and do not recommend or discourage any "
    "medication, supplement, dose, or treatment. Keep every number and citation "
    "exactly as before."
)


@dataclass
class Flag:
    category: Category
    label: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.category.value}: {self.label} — {self.excerpt!r}"


@dataclass
class GuardrailResult:
    flags: list[Flag] = field(default_factory=list)
    rewritten: bool = False
    disclaimer_added: bool = False

    @property
    def blocked(self) -> bool:
        """True when something the guard is meant to prevent survived."""
        return any(f.category in (Category.DIAGNOSIS, Category.TREATMENT,
                                  Category.UNCITED)
                   for f in self.flags)

    @property
    def categories(self) -> list[str]:
        return sorted({f.category.value for f in self.flags})


def _excerpt(text: str, match: re.Match, width: int = 60) -> str:
    start = max(0, match.start() - 10)
    end = min(len(text), match.end() + width // 2)
    return " ".join(text[start:end].split())


def _units(text: str) -> list[str]:
    """Sentences and lines, so a bullet or a table row is judged on its own.

    A parenthetical citation at the end of a sentence stays inside it, which
    is what lets "(Diabetes Care, 2024, PMID 42609254)" vouch for the claim
    it follows and for nothing else.
    """
    return [u.strip() for u in _UNIT_BOUNDARY.split(text) if u and u.strip()]


def _uncited_flags(text: str, returned_pmids: frozenset[str]) -> list[Flag]:
    flags: list[Flag] = []
    for unit in _units(text):
        for label, pattern in UNCITED_PATTERNS:
            match = re.search(pattern, unit, re.IGNORECASE)
            if not match:
                continue
            before = unit[max(0, match.start() - 60):match.start()]
            if _REPORTING_CONTEXT.search(before):
                break  # reporting the user's own document, not a claim
            if _PERSONAL_CONTEXT.search(unit[max(0, match.start() - 40):match.start()]):
                break  # "your A1c is ... above the printed range": theirs, not a claim
            cited = {m.group(1) for m in _PMID.finditer(unit)}
            if cited & returned_pmids:
                break  # a report of a finding the tool returned this turn
            if cited:
                flags.append(Flag(Category.UNCITED,
                                  "cites a PMID the literature tool did not return",
                                  _excerpt(unit, match)))
            else:
                flags.append(Flag(Category.UNCITED, label, _excerpt(unit, match)))
            break  # one flag per unit
    return flags


def check(text: str, *, used_tools: bool = True,
          returned_pmids: frozenset[str] = frozenset()) -> list[Flag]:
    """Scan a finished answer. Returns every flag raised, possibly empty.

    `returned_pmids` is the set of PMIDs the literature tool returned this
    turn. A general threshold passes only in a sentence that cites one of
    them; the default is the empty set, so a caller that says nothing gets
    the guard rather than an exemption.
    """
    flags: list[Flag] = []
    for category, label, pattern in PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            flags.append(Flag(category, label, _excerpt(text, match)))

    flags.extend(_uncited_flags(text, frozenset(returned_pmids)))

    # An answer that declines *and* looked nothing up is withholding, not
    # protecting. With tool results present, declining to interpret is correct.
    if not used_tools:
        for pattern in REFUSAL_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                flags.append(Flag(Category.REFUSAL,
                                  "declined without consulting the data",
                                  _excerpt(text, match)))
                break
    return flags


def needs_disclaimer(tools_used: list[str]) -> bool:
    """Plan §5: responses touching lab values carry a standing disclaimer.

    Scoped to lab values rather than every answer. A step count with a
    disclaimer stapled to it trains the reader to skip the disclaimer, which
    costs exactly the cases where it matters.
    """
    return "get_lab_trend" in tools_used


def apply(text: str, *, tools_used: list[str],
          returned_pmids: frozenset[str] = frozenset(),
          rewrite: "callable | None" = None) -> tuple[str, GuardrailResult]:
    """Run the guard over an answer and return the text to show the user.

    `rewrite` is an optional callable taking an instruction and returning a new
    answer; when supplied, one rewrite is attempted for a flagged response. A
    single attempt on purpose — if the model reproduces the problem twice, more
    attempts are unlikely to help and the user is left waiting.
    """
    result = GuardrailResult()
    flags = check(text, used_tools=bool(tools_used),
                  returned_pmids=returned_pmids)
    result.flags = list(flags)

    serious = [f for f in flags if f.category is not Category.REFUSAL]
    if serious and rewrite is not None:
        if any(f.category is Category.UNCITED for f in serious):
            instruction = UNCITED_REWRITE
        else:
            what = " and ".join(sorted({f.label for f in serious
                                        if f.category is not Category.UNCITED}))
            instruction = REWRITE_INSTRUCTION.format(what=what)
        try:
            revised = rewrite(instruction)
        except Exception:  # noqa: BLE001 - a failed rewrite must not lose the answer
            revised = ""
        if revised.strip():
            recheck = check(revised, used_tools=bool(tools_used),
                            returned_pmids=returned_pmids)
            still_serious = [f for f in recheck
                             if f.category is not Category.REFUSAL]
            if len(still_serious) < len(serious):
                text = revised
                result.rewritten = True
                result.flags = list(recheck)
                serious = still_serious

    if serious:
        # The rewrite did not clear it. Say so rather than presenting the
        # sentence as though it had passed a check.
        text = (f"{text}\n\n> **Note:** part of this answer reads as medical "
                f"interpretation, which this tool is not able to provide "
                f"reliably. Treat the values and citations as the useful part, "
                f"and take the interpretation to your clinician.")

    if needs_disclaimer(tools_used):
        text = f"{text}\n\n_{DISCLAIMER}_"
        result.disclaimer_added = True

    return text, result
