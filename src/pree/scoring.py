"""The Pree scoring core: an explainable, confidence-tiered threat score.

Pree answers one question for a Protect and Defend operator: which satellites hold this
protected asset at risk, and why. The score is a weighted blend of the evidence that is
actually present, never of assumed values.

The governing rule is that a missing input is never treated as a zero, a mean, or a safe
default. An absent indicator is excluded from the weighting and reported as
"TBC, re-verify", and the confidence tier falls to reflect how much evidence was absent.
Defaulting an unknown to a number would fabricate a figure and produce a confident score
from no data, which is the failure mode this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

UNKNOWN_MARKER = "TBC, re-verify"

# Weights sum to 1.0 across the full evidence set. When an indicator is absent its weight is
# removed from the denominator, so the score stays on a 0 to 100 scale from partial evidence
# rather than being silently dragged towards zero.
WEIGHTS: dict[str, float] = {
    "approach_geometry": 0.30,
    "relative_velocity": 0.20,
    "manoeuvre_cadence": 0.25,
    "photometric_anomaly": 0.15,
    "rf_activity": 0.10,
}

# Normalisation bounds, each a documented operational judgement rather than a magic number.
CLOSEST_APPROACH_FLOOR_KM = 1.0
CLOSEST_APPROACH_CEILING_KM = 500.0
CONTROLLABLE_VELOCITY_CEILING_KMS = 2.0
CADENCE_DEVIATION_CEILING = 4.0
PHOTOMETRIC_DEVIATION_CEILING = 3.0


class ConfidenceTier(StrEnum):
    """How much of the evidence set was actually present, not how alarming the score is."""

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INSUFFICIENT = "insufficient"


HIGH_CONFIDENCE_FLOOR = 0.85
MODERATE_CONFIDENCE_FLOOR = 0.60
LOW_CONFIDENCE_FLOOR = 0.30


@dataclass(frozen=True, slots=True)
class ThreatIndicators:
    """Evidence about one candidate object against one protected asset.

    Every field is optional. None means the indicator was not observed, which is a different
    statement from an observed value of zero and is scored differently.
    """

    closest_approach_km: float | None = None
    relative_velocity_kms: float | None = None
    manoeuvres_in_window: int | None = None
    baseline_manoeuvres: float | None = None
    photometric_sigma: float | None = None
    rf_emissions_detected: bool | None = None


@dataclass(frozen=True, slots=True)
class Contribution:
    """One indicator's contribution to the score, with the reason it scored that way."""

    indicator: str
    weight: float
    normalised: float | None
    rationale: str

    @property
    def present(self) -> bool:
        return self.normalised is not None


@dataclass(frozen=True, slots=True)
class Assessment:
    """The explainable result: a score, a tier, and the evidence behind both."""

    score: float
    confidence: ConfidenceTier
    evidence_coverage: float
    contributions: list[Contribution] = field(default_factory=list)

    @property
    def missing_indicators(self) -> list[str]:
        return [c.indicator for c in self.contributions if not c.present]


def _clamp_unit(value: float) -> float:
    """Clamp to the 0 to 1 range so an out-of-range reading cannot skew the blend."""
    return max(0.0, min(1.0, value))


def _score_approach(km: float | None) -> Contribution:
    """Closer approach means higher risk, on a linear ramp between the documented bounds."""
    weight = WEIGHTS["approach_geometry"]
    if km is None:
        return Contribution("approach_geometry", weight, None, UNKNOWN_MARKER)
    span = CLOSEST_APPROACH_CEILING_KM - CLOSEST_APPROACH_FLOOR_KM
    normalised = _clamp_unit((CLOSEST_APPROACH_CEILING_KM - km) / span)
    return Contribution(
        "approach_geometry",
        weight,
        normalised,
        f"closest approach {km:.1f} km against a {CLOSEST_APPROACH_CEILING_KM:.0f} km "
        f"attention threshold",
    )


def _score_velocity(kms: float | None) -> Contribution:
    """A low relative velocity implies a controllable, co-orbital approach, not a flyby."""
    weight = WEIGHTS["relative_velocity"]
    if kms is None:
        return Contribution("relative_velocity", weight, None, UNKNOWN_MARKER)
    normalised = _clamp_unit(
        (CONTROLLABLE_VELOCITY_CEILING_KMS - kms) / CONTROLLABLE_VELOCITY_CEILING_KMS
    )
    return Contribution(
        "relative_velocity",
        weight,
        normalised,
        f"relative velocity {kms:.2f} km/s; below "
        f"{CONTROLLABLE_VELOCITY_CEILING_KMS:.1f} km/s indicates a controllable approach",
    )


def _score_cadence(observed: int | None, baseline: float | None) -> Contribution:
    """Manoeuvre cadence above an object's own baseline is the strongest behavioural signal.

    Both the observation and the baseline are required. A cadence count with no baseline
    cannot be judged deviant, so it is reported absent rather than compared to an invented
    baseline.
    """
    weight = WEIGHTS["manoeuvre_cadence"]
    if observed is None or baseline is None:
        return Contribution("manoeuvre_cadence", weight, None, UNKNOWN_MARKER)
    excess = max(0.0, observed - baseline)
    normalised = _clamp_unit(excess / CADENCE_DEVIATION_CEILING)
    return Contribution(
        "manoeuvre_cadence",
        weight,
        normalised,
        f"{observed} manoeuvres against a baseline of {baseline:.1f}, an excess of {excess:.1f}",
    )


def _score_photometric(sigma: float | None) -> Contribution:
    """A photometric departure from an object's own signature suggests a changed shape."""
    weight = WEIGHTS["photometric_anomaly"]
    if sigma is None:
        return Contribution("photometric_anomaly", weight, None, UNKNOWN_MARKER)
    normalised = _clamp_unit(abs(sigma) / PHOTOMETRIC_DEVIATION_CEILING)
    return Contribution(
        "photometric_anomaly",
        weight,
        normalised,
        f"photometric departure of {abs(sigma):.2f} sigma from the object's own baseline",
    )


def _score_rf(detected: bool | None) -> Contribution:
    """RF emissions near the protected asset's band are a binary corroborating signal."""
    weight = WEIGHTS["rf_activity"]
    if detected is None:
        return Contribution("rf_activity", weight, None, UNKNOWN_MARKER)
    rationale = (
        "RF emissions detected in the protected asset's band"
        if detected
        else "no RF emissions detected in the protected asset's band"
    )
    return Contribution("rf_activity", weight, 1.0 if detected else 0.0, rationale)


def confidence_for(coverage: float) -> ConfidenceTier:
    """Map evidence coverage to a confidence tier. Coverage is weight present, not count."""
    if coverage >= HIGH_CONFIDENCE_FLOOR:
        return ConfidenceTier.HIGH
    if coverage >= MODERATE_CONFIDENCE_FLOOR:
        return ConfidenceTier.MODERATE
    if coverage >= LOW_CONFIDENCE_FLOOR:
        return ConfidenceTier.LOW
    return ConfidenceTier.INSUFFICIENT


def assess(indicators: ThreatIndicators) -> Assessment:
    """Score one candidate against one protected asset, explainably.

    With no evidence at all the score is 0.0 at INSUFFICIENT confidence. That pairing is the
    honest reading of an empty input set: it asserts nothing, and the tier says so.
    """
    contributions = [
        _score_approach(indicators.closest_approach_km),
        _score_velocity(indicators.relative_velocity_kms),
        _score_cadence(indicators.manoeuvres_in_window, indicators.baseline_manoeuvres),
        _score_photometric(indicators.photometric_sigma),
        _score_rf(indicators.rf_emissions_detected),
    ]
    present = [c for c in contributions if c.normalised is not None]
    available_weight = sum(c.weight for c in present)
    total_weight = sum(WEIGHTS.values())
    coverage = available_weight / total_weight if total_weight else 0.0

    if not present or available_weight == 0.0:
        return Assessment(0.0, ConfidenceTier.INSUFFICIENT, 0.0, contributions)

    blended = sum(c.weight * (c.normalised or 0.0) for c in present) / available_weight
    return Assessment(
        score=round(blended * 100.0, 1),
        confidence=confidence_for(coverage),
        evidence_coverage=round(coverage, 3),
        contributions=contributions,
    )
