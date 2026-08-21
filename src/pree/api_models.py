"""Request and response models. Boundary validation happens here, before any handler runs.

Every field carries an explicit range. An out-of-range or unknown field is rejected with 422
by the framework rather than coerced, which is the fail-closed reading of untrusted input.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_ID_LENGTH = 64
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class IndicatorPayload(BaseModel):
    """Observed evidence for one candidate object against one protected asset.

    Every indicator is optional because real Space Domain Awareness evidence arrives partial.
    An omitted field means not observed, and is excluded from the score rather than defaulted.
    """

    model_config = ConfigDict(extra="forbid")

    closest_approach_km: float | None = Field(default=None, ge=0.0, le=1_000_000.0)
    relative_velocity_kms: float | None = Field(default=None, ge=0.0, le=100.0)
    manoeuvres_in_window: int | None = Field(default=None, ge=0, le=10_000)
    baseline_manoeuvres: float | None = Field(default=None, ge=0.0, le=10_000.0)
    photometric_sigma: float | None = Field(default=None, ge=-100.0, le=100.0)
    rf_emissions_detected: bool | None = None


class AssessRequest(BaseModel):
    """One assessment request: a protected asset, a candidate, and the evidence."""

    model_config = ConfigDict(extra="forbid")

    protected_asset_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH, pattern=ID_PATTERN)
    candidate_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH, pattern=ID_PATTERN)
    indicators: IndicatorPayload = Field(default_factory=IndicatorPayload)


class ContributionOut(BaseModel):
    """One indicator's contribution, so the operator can see why the score is what it is."""

    indicator: str
    weight: float
    normalised: float | None
    rationale: str


class AssessResponse(BaseModel):
    """The explainable assessment returned to the operator."""

    protected_asset_id: str
    candidate_id: str
    score: float
    confidence: str
    evidence_coverage: float
    missing_indicators: list[str]
    contributions: list[ContributionOut]
    schema_version: int
