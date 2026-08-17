"""Analyte registry: canonical names, aliases, and unit expectations.

Lab reports name the same measurement a dozen ways — "Hemoglobin A1c", "HbA1c",
"A1C", "Glycohemoglobin" are one analyte, and a trend query that treats them as
four different things is useless. This module maps printed names onto a stable
`key` so `get_lab_trend("hba1c")` spans reports from different labs.

The registry is intentionally partial and additive. An unrecognized analyte is
*not* discarded: it gets a slugified key derived from its printed name, so it is
still stored, still citable, and still trendable within one lab's naming. Only
cross-lab merging depends on being in the registry.

`expected_units` is advisory. It is used to flag a value whose unit is wildly
unlike the usual one (mg/dL vs mmol/L for glucose is a 18x difference), which is
the kind of thing that silently corrupts a trend line. It is never used to
convert — unit conversion without knowing the assay is how you get confidently
wrong numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Analyte:
    key: str
    label: str
    panel: str
    aliases: tuple[str, ...] = ()
    expected_units: tuple[str, ...] = field(default=())


REGISTRY: tuple[Analyte, ...] = (
    # --- Lipid panel ---
    Analyte("cholesterol_total", "Total cholesterol", "lipids",
            ("cholesterol", "cholesterol total", "total cholesterol",
             "cholesterol, total"), ("mg/dl", "mmol/l")),
    Analyte("hdl", "HDL cholesterol", "lipids",
            ("hdl", "hdl cholesterol", "hdl-c", "cholesterol hdl",
             "hdl cholesterol (direct)"), ("mg/dl", "mmol/l")),
    Analyte("ldl", "LDL cholesterol", "lipids",
            ("ldl", "ldl cholesterol", "ldl-c", "ldl cholesterol calc",
             "ldl cholesterol (calc)", "ldl chol calc (nih)"), ("mg/dl", "mmol/l")),
    Analyte("triglycerides", "Triglycerides", "lipids",
            ("triglycerides", "trigs", "triglyceride"), ("mg/dl", "mmol/l")),
    Analyte("non_hdl", "Non-HDL cholesterol", "lipids",
            ("non hdl cholesterol", "non-hdl cholesterol", "non hdl chol"),
            ("mg/dl",)),
    Analyte("chol_hdl_ratio", "Cholesterol/HDL ratio", "lipids",
            ("chol/hdl ratio", "cholesterol/hdl ratio", "chol/hdl"), ("ratio",)),

    # --- Metabolic ---
    Analyte("glucose", "Glucose", "metabolic",
            ("glucose", "glucose fasting", "fasting glucose", "glucose, fasting",
             "glucose serum"), ("mg/dl", "mmol/l")),
    Analyte("hba1c", "Hemoglobin A1c", "metabolic",
            ("hba1c", "hemoglobin a1c", "a1c", "glycohemoglobin",
             "hgb a1c", "hemoglobin a1c (hba1c)"), ("%",)),
    Analyte("insulin", "Insulin", "metabolic",
            ("insulin", "insulin fasting"), ("uiu/ml", "µiu/ml")),
    Analyte("uric_acid", "Uric acid", "metabolic", ("uric acid",), ("mg/dl",)),

    # --- Comprehensive metabolic panel ---
    Analyte("bun", "BUN", "cmp",
            ("bun", "urea nitrogen", "blood urea nitrogen",
             "urea nitrogen (bun)"), ("mg/dl",)),
    Analyte("creatinine", "Creatinine", "cmp",
            ("creatinine", "creatinine serum"), ("mg/dl",)),
    Analyte("egfr", "eGFR", "cmp",
            ("egfr", "gfr estimated", "egfr non-afr. american",
             "estimated gfr"), ("ml/min/1.73",)),
    Analyte("sodium", "Sodium", "cmp", ("sodium", "na"), ("mmol/l", "meq/l")),
    Analyte("potassium", "Potassium", "cmp", ("potassium", "k"),
            ("mmol/l", "meq/l")),
    Analyte("chloride", "Chloride", "cmp", ("chloride", "cl"),
            ("mmol/l", "meq/l")),
    Analyte("co2", "CO2", "cmp",
            ("co2", "carbon dioxide", "bicarbonate", "co2, total"),
            ("mmol/l", "meq/l")),
    Analyte("calcium", "Calcium", "cmp", ("calcium", "ca"), ("mg/dl",)),
    Analyte("protein_total", "Total protein", "cmp",
            ("total protein", "protein total", "protein, total"), ("g/dl",)),
    Analyte("albumin", "Albumin", "cmp", ("albumin",), ("g/dl",)),
    Analyte("globulin", "Globulin", "cmp",
            ("globulin", "globulin total", "globulin, total"), ("g/dl",)),
    Analyte("bilirubin_total", "Total bilirubin", "cmp",
            ("bilirubin total", "total bilirubin", "bilirubin, total"),
            ("mg/dl",)),
    Analyte("alk_phos", "Alkaline phosphatase", "cmp",
            ("alkaline phosphatase", "alk phos", "alp"), ("iu/l", "u/l")),
    Analyte("ast", "AST", "cmp", ("ast", "ast (sgot)", "sgot"), ("iu/l", "u/l")),
    Analyte("alt", "ALT", "cmp", ("alt", "alt (sgpt)", "sgpt"), ("iu/l", "u/l")),

    # --- CBC ---
    Analyte("wbc", "White blood cells", "cbc",
            ("wbc", "white blood cell count", "leukocytes"),
            ("x10e3/ul", "k/ul", "10*3/ul")),
    Analyte("rbc", "Red blood cells", "cbc",
            ("rbc", "red blood cell count", "erythrocytes"),
            ("x10e6/ul", "m/ul")),
    Analyte("hemoglobin", "Hemoglobin", "cbc", ("hemoglobin", "hgb", "hb"),
            ("g/dl",)),
    Analyte("hematocrit", "Hematocrit", "cbc", ("hematocrit", "hct"), ("%",)),
    Analyte("mcv", "MCV", "cbc", ("mcv",), ("fl",)),
    Analyte("mch", "MCH", "cbc", ("mch",), ("pg",)),
    Analyte("mchc", "MCHC", "cbc", ("mchc",), ("g/dl",)),
    Analyte("rdw", "RDW", "cbc", ("rdw",), ("%",)),
    Analyte("platelets", "Platelets", "cbc",
            ("platelets", "platelet count", "plt"), ("x10e3/ul", "k/ul")),
    Analyte("neutrophils", "Neutrophils", "cbc",
            ("neutrophils", "neutrophils absolute", "abs neutrophils"), ("%",)),
    Analyte("lymphocytes", "Lymphocytes", "cbc",
            ("lymphocytes", "lymphs", "lymphocytes absolute"), ("%",)),

    # --- Thyroid ---
    Analyte("tsh", "TSH", "thyroid",
            ("tsh", "thyroid stimulating hormone"), ("uiu/ml", "µiu/ml", "miu/l")),
    Analyte("free_t4", "Free T4", "thyroid",
            ("free t4", "t4 free", "ft4", "free thyroxine"), ("ng/dl",)),
    Analyte("free_t3", "Free T3", "thyroid",
            ("free t3", "t3 free", "ft3"), ("pg/ml",)),

    # --- Vitamins / minerals / inflammation ---
    Analyte("vitamin_d", "Vitamin D, 25-OH", "vitamins",
            ("vitamin d", "vitamin d 25 hydroxy", "vitamin d, 25-hydroxy",
             "25-oh vitamin d", "vit d 25 hydroxy"), ("ng/ml",)),
    Analyte("vitamin_b12", "Vitamin B12", "vitamins",
            ("vitamin b12", "b12", "cobalamin"), ("pg/ml",)),
    Analyte("folate", "Folate", "vitamins", ("folate", "folic acid"), ("ng/ml",)),
    Analyte("ferritin", "Ferritin", "vitamins", ("ferritin",), ("ng/ml",)),
    Analyte("iron", "Iron", "vitamins", ("iron", "iron serum", "iron, serum"),
            ("ug/dl", "µg/dl")),
    Analyte("magnesium", "Magnesium", "vitamins", ("magnesium", "mg"), ("mg/dl",)),
    Analyte("crp", "C-reactive protein", "inflammation",
            ("crp", "c-reactive protein", "c reactive protein"), ("mg/l",)),
    Analyte("hs_crp", "hs-CRP", "inflammation",
            ("hs-crp", "hs crp", "high sensitivity crp",
             "c-reactive protein, cardiac"), ("mg/l",)),

    # --- Hormones ---
    Analyte("testosterone_total", "Testosterone, total", "hormones",
            ("testosterone", "testosterone total", "testosterone, total"),
            ("ng/dl",)),
    Analyte("psa", "PSA", "hormones",
            ("psa", "prostate specific antigen", "psa total"), ("ng/ml",)),
    Analyte("cortisol", "Cortisol", "hormones", ("cortisol",), ("ug/dl",)),
)

def normalize_name(raw: str) -> str:
    """Reduce a printed analyte name to a comparable form.

    Strips the punctuation and qualifiers labs sprinkle around names so
    "LDL Chol Calc (NIH)" and "LDL Cholesterol (Calc)" land on the same string.
    """
    text = raw.strip().lower()
    text = re.sub(r"[‐-―]", "-", text)          # unicode dashes
    text = re.sub(r"\b(serum|plasma|blood|whole blood)\b", " ", text)
    text = re.sub(r"[,:;]", " ", text)
    text = re.sub(r"[().]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Alias keys are stored *normalized*, because lookups arrive normalized. Storing
# them raw silently breaks every alias containing punctuation — "AST (SGOT)" was
# indexed under "ast (sgot)" while every lookup asked for "ast sgot", so the
# whole entry was dead weight.
_BY_ALIAS: dict[str, Analyte] = {}
for _a in REGISTRY:
    _BY_ALIAS[_a.key] = _a
    _BY_ALIAS[normalize_name(_a.label)] = _a
    for _alias in _a.aliases:
        _BY_ALIAS[normalize_name(_alias)] = _a


def slugify(raw: str) -> str:
    text = normalize_name(raw)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:60] or "unknown"


# Letters OCR commonly substitutes for digits inside short analyte names:
# "A1c" is routinely read as "Alc" or "AIc". Applied only as a last-resort
# fallback, and only to short mixed tokens, so ordinary words are untouched.
_OCR_DIGIT_LOOKALIKES = str.maketrans({"l": "1", "i": "1", "o": "0", "s": "5"})


def _ocr_variant(normalized: str) -> str | None:
    """A digit-corrected variant of a name, or None if nothing would change.

    Only short tokens are touched. Folding letters to digits across a whole name
    would mangle real words ("alkaline" -> "a1ka1ine") and could map one analyte
    onto a different one — a far worse outcome than failing to merge a trend.
    Five characters covers the names that actually embed digits and so are the
    ones OCR mangles: a1c, hba1c, b12, ft4, vo2, co2.

    This is only ever consulted after an exact match has failed, so a variant
    that collides with nothing simply yields no result.
    """
    tokens = normalized.split()
    changed = False
    out = []
    for token in tokens:
        if len(token) <= 5 and any(c.isalpha() for c in token):
            candidate = token.translate(_OCR_DIGIT_LOOKALIKES)
            if candidate != token:
                changed = True
                out.append(candidate)
                continue
        out.append(token)
    return " ".join(out) if changed else None


def lookup(raw: str) -> Analyte | None:
    """Resolve a printed analyte name to a registry entry, or None."""
    normalized = normalize_name(raw)
    hit = _BY_ALIAS.get(normalized)
    if hit is not None:
        return hit

    # "LDL Cholesterol (Calc)" -> also try without a trailing qualifier word.
    trimmed = re.sub(r"\b(calc|calculated|direct|total|serum|level)\b", "",
                     normalized).strip()
    trimmed = re.sub(r"\s+", " ", trimmed)
    hit = _BY_ALIAS.get(trimmed)
    if hit is not None:
        return hit

    # Last resort: an OCR-scanned report writing "Hemoglobin Alc" for "A1c"
    # would otherwise never join the trend built from text-layer reports.
    for candidate in (normalized, trimmed):
        variant = _ocr_variant(candidate)
        if variant and variant in _BY_ALIAS:
            return _BY_ALIAS[variant]
    return None


def resolve_key(raw: str) -> tuple[str, str | None]:
    """Return (key, panel) for a printed analyte name.

    Unregistered analytes get a slug key so they remain queryable; only
    cross-lab alias merging is lost.
    """
    hit = lookup(raw)
    if hit is not None:
        return hit.key, hit.panel
    return slugify(raw), None


def label_for(key: str) -> str:
    hit = _BY_ALIAS.get(key)
    return hit.label if hit else key.replace("_", " ")


def unit_looks_wrong(key: str, unit: str | None) -> bool:
    """True when a unit is present, the analyte is known, and they disagree.

    Advisory only — surfaced to the user, never used to convert or discard.
    """
    if not unit:
        return False
    hit = _BY_ALIAS.get(key)
    if hit is None or not hit.expected_units:
        return False
    seen = unit.strip().lower().replace("µ", "u")
    return not any(seen.startswith(exp.replace("µ", "u")) for exp in hit.expected_units)


def search(term: str, limit: int = 10) -> list[Analyte]:
    needle = normalize_name(term)
    if not needle:
        return []
    out = []
    for analyte in REGISTRY:
        haystack = " ".join((analyte.key, analyte.label, *analyte.aliases)).lower()
        if needle in haystack:
            out.append(analyte)
        if len(out) >= limit:
            break
    return out
