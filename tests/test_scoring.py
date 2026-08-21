"""The scoring core. The central property under test is that an unknown is never invented."""

from __future__ import annotations

import pytest

from pree.scoring import (
    UNKNOWN_MARKER,
    WEIGHTS,
    ConfidenceTier,
    ThreatIndicators,
    assess,
    confidence_for,
)

FULL_EVIDENCE = ThreatIndicators(
    closest_approach_km=5.0,
    relative_velocity_kms=0.1,
    manoeuvres_in_window=6,
    baseline_manoeuvres=1.0,
    photometric_sigma=2.5,
    rf_emissions_detected=True,
)


def test_weights_sum_to_one_so_full_evidence_is_a_complete_blend() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_no_evidence_scores_zero_at_insufficient_confidence() -> None:
    result = assess(ThreatIndicators())
    assert result.score == 0.0
    assert result.confidence is ConfidenceTier.INSUFFICIENT
    assert result.evidence_coverage == 0.0
    assert len(result.missing_indicators) == len(WEIGHTS)


def test_every_absent_indicator_is_marked_unknown_not_defaulted() -> None:
    result = assess(ThreatIndicators(closest_approach_km=10.0))
    absent = [c for c in result.contributions if not c.present]
    assert absent, "expected absent indicators for a partial input"
    assert all(c.rationale == UNKNOWN_MARKER for c in absent)
    assert all(c.normalised is None for c in absent)


def test_a_close_slow_manoeuvring_object_scores_high_at_high_confidence() -> None:
    result = assess(FULL_EVIDENCE)
    assert result.score > 80.0
    assert result.confidence is ConfidenceTier.HIGH
    assert result.evidence_coverage == pytest.approx(1.0)
    assert result.missing_indicators == []


def test_a_distant_fast_quiet_object_scores_low_at_high_confidence() -> None:
    result = assess(
        ThreatIndicators(
            closest_approach_km=480.0,
            relative_velocity_kms=7.5,
            manoeuvres_in_window=0,
            baseline_manoeuvres=0.0,
            photometric_sigma=0.0,
            rf_emissions_detected=False,
        )
    )
    assert result.score < 10.0
    assert result.confidence is ConfidenceTier.HIGH


def test_partial_evidence_stays_on_the_same_scale_rather_than_collapsing() -> None:
    """A single strong indicator must not be diluted towards zero by the absent ones."""
    result = assess(ThreatIndicators(closest_approach_km=1.0))
    assert result.score == pytest.approx(100.0)
    # One indicator of five is 0.30 evidence coverage, which is the documented LOW floor.
    # The score is undiluted; the tier is what carries the warning that most evidence is absent.
    assert result.confidence is ConfidenceTier.LOW
    assert result.evidence_coverage == pytest.approx(0.30)


def test_confidence_falls_as_evidence_coverage_falls() -> None:
    full = assess(FULL_EVIDENCE)
    partial = assess(
        ThreatIndicators(
            closest_approach_km=5.0,
            relative_velocity_kms=0.1,
            manoeuvres_in_window=6,
            baseline_manoeuvres=1.0,
        )
    )
    sparse = assess(ThreatIndicators(closest_approach_km=5.0))
    assert full.evidence_coverage > partial.evidence_coverage > sparse.evidence_coverage
    assert full.confidence is ConfidenceTier.HIGH
    assert partial.confidence is ConfidenceTier.MODERATE
    assert sparse.confidence is ConfidenceTier.LOW


def test_cadence_needs_both_the_count_and_the_baseline() -> None:
    """A count with no baseline cannot be judged deviant, so it is reported absent."""
    result = assess(ThreatIndicators(manoeuvres_in_window=9))
    cadence = next(c for c in result.contributions if c.indicator == "manoeuvre_cadence")
    assert cadence.present is False
    assert cadence.rationale == UNKNOWN_MARKER


def test_readings_beyond_the_documented_bounds_are_clamped_not_extrapolated() -> None:
    nearer_than_floor = assess(ThreatIndicators(closest_approach_km=0.0))
    beyond_ceiling = assess(ThreatIndicators(closest_approach_km=100_000.0))
    assert nearer_than_floor.score == pytest.approx(100.0)
    assert beyond_ceiling.score == pytest.approx(0.0)


def test_negative_photometric_sigma_is_as_anomalous_as_positive() -> None:
    dimmer = assess(ThreatIndicators(photometric_sigma=-2.0))
    brighter = assess(ThreatIndicators(photometric_sigma=2.0))
    assert dimmer.score == brighter.score


def test_an_observed_false_differs_from_an_absent_reading() -> None:
    observed_false = assess(ThreatIndicators(rf_emissions_detected=False))
    absent = assess(ThreatIndicators())
    assert observed_false.evidence_coverage > absent.evidence_coverage
    assert observed_false.confidence is not ConfidenceTier.HIGH


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (1.0, ConfidenceTier.HIGH),
        (0.85, ConfidenceTier.HIGH),
        (0.84, ConfidenceTier.MODERATE),
        (0.60, ConfidenceTier.MODERATE),
        (0.59, ConfidenceTier.LOW),
        (0.30, ConfidenceTier.LOW),
        (0.29, ConfidenceTier.INSUFFICIENT),
        (0.0, ConfidenceTier.INSUFFICIENT),
    ],
)
def test_confidence_tier_boundaries(coverage: float, expected: ConfidenceTier) -> None:
    assert confidence_for(coverage) is expected


def test_every_contribution_carries_a_rationale_so_the_score_is_explainable() -> None:
    result = assess(FULL_EVIDENCE)
    assert len(result.contributions) == len(WEIGHTS)
    assert all(c.rationale for c in result.contributions)
