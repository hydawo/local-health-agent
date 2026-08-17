"""Metric registry: friendly names, HealthKit identifiers, aggregation semantics.

Two things live here that the rest of the codebase should not re-derive:

1. **Aliases.** Users (and later, the LLM's tool calls) say "resting-hr", not
   "HKQuantityTypeIdentifierRestingHeartRate".

2. **Aggregation kind.** HealthKit quantity types are either *cumulative* (steps,
   energy, distance — samples partition an interval, so a day is their SUM) or
   *discrete* (heart rate, weight, VO2max — samples are point observations, so a
   day is their MEAN). Using the wrong one silently produces nonsense, e.g.
   "average steps per day = 41" because it averaged individual samples instead of
   summing them. `CATEGORY` types (sleep, stand hours) carry a string value
   instead of a number; their meaningful aggregate is total *duration* per
   category value.

The registry is intentionally partial. Any HealthKit type present in the export
is queryable by its full identifier; the registry only adds a short name and the
correct default aggregation for the common ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

HK_PREFIX_QUANTITY = "HKQuantityTypeIdentifier"
HK_PREFIX_CATEGORY = "HKCategoryTypeIdentifier"


class Kind(str, Enum):
    CUMULATIVE = "cumulative"
    DISCRETE = "discrete"
    CATEGORY = "category"


@dataclass(frozen=True)
class Metric:
    identifier: str
    aliases: tuple[str, ...]
    kind: Kind
    label: str

    @property
    def default_agg(self) -> str:
        if self.kind is Kind.CUMULATIVE:
            return "sum"
        if self.kind is Kind.DISCRETE:
            return "avg"
        return "duration"


def _q(name: str, aliases: tuple[str, ...], kind: Kind, label: str) -> Metric:
    return Metric(HK_PREFIX_QUANTITY + name, aliases, kind, label)


def _c(name: str, aliases: tuple[str, ...], label: str) -> Metric:
    return Metric(HK_PREFIX_CATEGORY + name, aliases, Kind.CATEGORY, label)


REGISTRY: tuple[Metric, ...] = (
    # --- Activity (cumulative) ---
    _q("StepCount", ("steps", "step-count"), Kind.CUMULATIVE, "Steps"),
    _q("DistanceWalkingRunning", ("distance", "walk-run-distance"), Kind.CUMULATIVE,
       "Walking + running distance"),
    _q("DistanceCycling", ("cycling-distance",), Kind.CUMULATIVE, "Cycling distance"),
    _q("FlightsClimbed", ("flights", "stairs"), Kind.CUMULATIVE, "Flights climbed"),
    _q("ActiveEnergyBurned", ("active-energy", "active-calories"), Kind.CUMULATIVE,
       "Active energy burned"),
    _q("BasalEnergyBurned", ("basal-energy", "resting-calories"), Kind.CUMULATIVE,
       "Basal energy burned"),
    _q("AppleExerciseTime", ("exercise-time",), Kind.CUMULATIVE, "Exercise minutes"),
    _q("AppleStandTime", ("stand-time",), Kind.CUMULATIVE, "Stand minutes"),
    _q("TimeInDaylight", ("daylight",), Kind.CUMULATIVE, "Time in daylight"),

    # --- Cardio / vitals (discrete) ---
    _q("HeartRate", ("hr", "heart-rate"), Kind.DISCRETE, "Heart rate"),
    _q("RestingHeartRate", ("resting-hr", "resting-heart-rate"), Kind.DISCRETE,
       "Resting heart rate"),
    _q("WalkingHeartRateAverage", ("walking-hr",), Kind.DISCRETE,
       "Walking heart rate average"),
    _q("HeartRateVariabilitySDNN", ("hrv", "hrv-sdnn"), Kind.DISCRETE, "HRV (SDNN)"),
    _q("HeartRateRecoveryOneMinute", ("hr-recovery",), Kind.DISCRETE,
       "1-minute heart rate recovery"),
    _q("RespiratoryRate", ("respiratory-rate", "breathing-rate"), Kind.DISCRETE,
       "Respiratory rate"),
    _q("OxygenSaturation", ("spo2", "oxygen"), Kind.DISCRETE, "Blood oxygen"),
    _q("BloodPressureSystolic", ("systolic", "bp-systolic"), Kind.DISCRETE,
       "Blood pressure (systolic)"),
    _q("BloodPressureDiastolic", ("diastolic", "bp-diastolic"), Kind.DISCRETE,
       "Blood pressure (diastolic)"),
    _q("BodyTemperature", ("body-temp",), Kind.DISCRETE, "Body temperature"),
    _q("AppleSleepingWristTemperature", ("wrist-temp", "sleeping-wrist-temp"),
       Kind.DISCRETE, "Sleeping wrist temperature"),
    _q("VO2Max", ("vo2max", "vo2-max"), Kind.DISCRETE, "VO2 max"),
    _q("BloodGlucose", ("glucose", "blood-glucose"), Kind.DISCRETE, "Blood glucose"),

    # --- Body (discrete) ---
    _q("BodyMass", ("weight", "body-mass"), Kind.DISCRETE, "Body mass"),
    _q("BodyMassIndex", ("bmi",), Kind.DISCRETE, "Body mass index"),
    _q("BodyFatPercentage", ("body-fat",), Kind.DISCRETE, "Body fat percentage"),
    _q("LeanBodyMass", ("lean-mass",), Kind.DISCRETE, "Lean body mass"),
    _q("Height", ("height",), Kind.DISCRETE, "Height"),

    # --- Gait / mobility (discrete) ---
    _q("WalkingSpeed", ("walking-speed",), Kind.DISCRETE, "Walking speed"),
    _q("WalkingStepLength", ("step-length",), Kind.DISCRETE, "Walking step length"),
    _q("WalkingAsymmetryPercentage", ("walking-asymmetry",), Kind.DISCRETE,
       "Walking asymmetry"),
    _q("WalkingDoubleSupportPercentage", ("double-support",), Kind.DISCRETE,
       "Walking double support"),
    _q("AppleWalkingSteadiness", ("walking-steadiness",), Kind.DISCRETE,
       "Walking steadiness"),
    _q("StairAscentSpeed", ("stair-ascent-speed",), Kind.DISCRETE, "Stair ascent speed"),
    _q("StairDescentSpeed", ("stair-descent-speed",), Kind.DISCRETE,
       "Stair descent speed"),
    _q("SixMinuteWalkTestDistance", ("six-minute-walk",), Kind.DISCRETE,
       "Six-minute walk distance"),

    # --- Environment / audio (discrete) ---
    _q("EnvironmentalAudioExposure", ("environmental-audio",), Kind.DISCRETE,
       "Environmental audio exposure"),
    _q("HeadphoneAudioExposure", ("headphone-audio",), Kind.DISCRETE,
       "Headphone audio exposure"),

    # --- Nutrition (cumulative) ---
    _q("DietaryEnergyConsumed", ("calories-consumed", "dietary-energy"),
       Kind.CUMULATIVE, "Dietary energy consumed"),
    _q("DietaryProtein", ("protein",), Kind.CUMULATIVE, "Dietary protein"),
    _q("DietaryCarbohydrates", ("carbs", "carbohydrates"), Kind.CUMULATIVE,
       "Dietary carbohydrates"),
    _q("DietaryFatTotal", ("fat", "dietary-fat"), Kind.CUMULATIVE, "Dietary fat"),
    _q("DietaryFiber", ("fiber",), Kind.CUMULATIVE, "Dietary fiber"),
    _q("DietarySugar", ("sugar",), Kind.CUMULATIVE, "Dietary sugar"),
    _q("DietarySodium", ("sodium",), Kind.CUMULATIVE, "Dietary sodium"),
    _q("DietaryWater", ("water",), Kind.CUMULATIVE, "Dietary water"),
    _q("DietaryCaffeine", ("caffeine",), Kind.CUMULATIVE, "Dietary caffeine"),

    # --- Category types (aggregate by duration per category value) ---
    _c("SleepAnalysis", ("sleep",), "Sleep analysis"),
    _c("AppleStandHour", ("stand-hours",), "Stand hours"),
    _c("MindfulSession", ("mindful", "mindfulness"), "Mindful sessions"),
    _c("HighHeartRateEvent", ("high-hr-event",), "High heart rate events"),
    _c("AudioExposureEvent", ("audio-exposure-event",), "Audio exposure events"),
    _c("HandwashingEvent", ("handwashing",), "Handwashing events"),
)

_BY_ALIAS: dict[str, Metric] = {}
for _m in REGISTRY:
    _BY_ALIAS[_m.identifier.lower()] = _m
    for _a in _m.aliases:
        _BY_ALIAS[_a.lower()] = _m


def lookup(name: str) -> Metric | None:
    """Resolve a friendly alias or full HealthKit identifier to a Metric.

    Returns None for unknown names. Callers that want to query an unregistered
    HealthKit type should fall back to `unregistered()`.
    """
    key = name.strip().lower().replace("_", "-")
    hit = _BY_ALIAS.get(key)
    if hit is not None:
        return hit
    # Tolerate the identifier written without its prefix, e.g. "RestingHeartRate".
    for prefix in (HK_PREFIX_QUANTITY, HK_PREFIX_CATEGORY):
        hit = _BY_ALIAS.get((prefix + name.strip()).lower())
        if hit is not None:
            return hit
    return None


def unregistered(identifier: str) -> Metric:
    """Wrap an arbitrary HealthKit identifier with conservative defaults.

    Unknown quantity types are treated as discrete: averaging a cumulative type
    understates it visibly, while summing a discrete type produces a number that
    looks plausible and is completely wrong. Prefer the visible failure.
    """
    kind = Kind.CATEGORY if identifier.startswith(HK_PREFIX_CATEGORY) else Kind.DISCRETE
    label = identifier
    for prefix in (HK_PREFIX_QUANTITY, HK_PREFIX_CATEGORY):
        if identifier.startswith(prefix):
            label = identifier[len(prefix):]
            break
    return Metric(identifier, (), kind, label)


def resolve(name: str) -> Metric:
    """Resolve a name to a Metric, falling back to an unregistered identifier."""
    hit = lookup(name)
    if hit is not None:
        return hit
    return unregistered(name)


def suggest(name: str, limit: int = 5) -> list[Metric]:
    """Cheap substring suggestions for an unrecognized metric name."""
    key = name.strip().lower().replace("_", "-")
    if not key:
        return []
    out: list[Metric] = []
    for metric in REGISTRY:
        haystack = " ".join((metric.identifier, metric.label, *metric.aliases)).lower()
        if key in haystack:
            out.append(metric)
        if len(out) >= limit:
            break
    return out
