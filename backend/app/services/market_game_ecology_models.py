"""Optional research contracts; no field grants an order or position change."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EcologyMetric(ResearchContract):
    id: str
    label: str
    value: float | None = None
    unit: str
    sample_size: int = Field(default=0, ge=0)
    reason: str = ""


class EcologySector(ResearchContract):
    name: str
    member_count: int = Field(ge=0)
    amount_share: float = Field(ge=0, le=1)
    share_change: float | None = None
    breadth: float | None = Field(default=None, ge=0, le=1)
    avg_return: float | None = None
    relative_return: float | None = None
    status: str
    evidence: list[str]


class MarketEcology(ResearchContract):
    version: str
    shadow_only: Literal[True] = True
    scope: Literal["audited_daily_sample"] = "audited_daily_sample"
    state: Literal["expanding", "rotation", "concentrated", "contracting", "unconfirmed"]
    label: str
    as_of: str
    cutoff: str
    metadata_scope: str
    universe_version: str
    membership_version: str | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)
    comparison_date: str | None = None
    metrics: list[EcologyMetric]
    sectors: list[EcologySector]
    evidence: list[str]
    interpretation: str
    limitations: list[str]


class FeedbackCondition(ResearchContract):
    metric: str
    operator: Literal[">", ">=", "<", "<="]
    value: float
    description: str


class FeedbackHypothesis(ResearchContract):
    version: str
    id: str
    shadow_only: Literal[True] = True
    entry_authorized: Literal[False] = False
    kind: Literal["continuation", "repair", "unconfirmed"]
    label: str
    as_of: str
    cutoff: str
    observation_sessions: list[str]
    expires_at: str | None = None
    risk_appetite: float | None = Field(default=None, ge=0, le=100)
    panic_pressure: float | None = Field(default=None, ge=0, le=100)
    minimum_samples: int = Field(default=60, ge=60)
    confirmation: list[FeedbackCondition]
    invalidation: list[FeedbackCondition]
    interpretation: str
    alternative: str
    limitations: list[str]


class FeedbackRow(ResearchContract):
    symbol: str
    name: str
    observed_at: str | None = None
    status: Literal["supported", "contradicted", "pending", "unavailable", "expired"]
    price_direction: Literal["up", "down", "flat", "unknown"]
    feedback_type: Literal["reinforcing", "corrective", "unclear"]
    stage: Literal["strengthening", "repair", "fragile", "unconfirmed"]
    label: str
    evidence: list[str]
    reason: str


class FeedbackObservation(ResearchContract):
    version: str
    hypothesis_id: str | None = None
    shadow_only: Literal[True] = True
    entry_authorized: Literal[False] = False
    scope: Literal["frozen_plan_sample"] = "frozen_plan_sample"
    observed_at: str | None = None
    status: Literal["available", "limited", "unavailable"]
    summary: str
    rows: list[FeedbackRow]
    limitations: list[str]
