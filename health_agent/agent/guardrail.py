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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    DIAGNOSIS = "diagnosis"
    TREATMENT = "treatment"
    REFUSAL = "unhelpful_refusal"


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
        return any(f.category in (Category.DIAGNOSIS, Category.TREATMENT)
                   for f in self.flags)

    @property
    def categories(self) -> list[str]:
        return sorted({f.category.value for f in self.flags})


def _excerpt(text: str, match: re.Match, width: int = 60) -> str:
    start = max(0, match.start() - 10)
    end = min(len(text), match.end() + width // 2)
    return " ".join(text[start:end].split())


def check(text: str, *, used_tools: bool = True) -> list[Flag]:
    """Scan a finished answer. Returns every flag raised, possibly empty."""
    flags: list[Flag] = []
    for category, label, pattern in PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            flags.append(Flag(category, label, _excerpt(text, match)))

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
          rewrite: "callable | None" = None) -> tuple[str, GuardrailResult]:
    """Run the guard over an answer and return the text to show the user.

    `rewrite` is an optional callable taking an instruction and returning a new
    answer; when supplied, one rewrite is attempted for a flagged response. A
    single attempt on purpose — if the model reproduces the problem twice, more
    attempts are unlikely to help and the user is left waiting.
    """
    result = GuardrailResult()
    flags = check(text, used_tools=bool(tools_used))
    result.flags = list(flags)

    serious = [f for f in flags if f.category is not Category.REFUSAL]
    if serious and rewrite is not None:
        what = " and ".join(sorted({f.label for f in serious}))
        try:
            revised = rewrite(REWRITE_INSTRUCTION.format(what=what))
        except Exception:  # noqa: BLE001 - a failed rewrite must not lose the answer
            revised = ""
        if revised.strip():
            recheck = check(revised, used_tools=bool(tools_used))
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
