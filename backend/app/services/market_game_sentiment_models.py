"""Frozen zzshare short-term sentiment contract; optional for older reports."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SentimentMetric(_Contract):
    id: str
    label: str
    unit: Literal["score", "count", "ratio", "return"]
    value: float | None
    percentile: float | None = Field(default=None, ge=0, le=100)
    sample_size: int = Field(ge=0)


class LossGate(_Contract):
    score: float | None = Field(default=None, ge=0, le=100)
    level: Literal["none", "caution", "veto", "unavailable"]
    components: list[str] = Field(default_factory=list)
    caution: float = Field(ge=0, le=100)
    veto: float = Field(ge=0, le=100)


class CrossCheck(_Contract):
    status: Literal["matched", "mismatched", "unavailable"]
    reason: str
    local_limit_up: float | None = None
    vendor_limit_up: float | None = None
    local_limit_down: float | None = None
    vendor_limit_down: float | None = None


class SentimentPoint(_Contract):
    date: str
    loss_gate: float | None = Field(default=None, ge=0, le=100)
    earn_effect: float | None = None
    lose_effect: float | None = None


class SentimentContext(_Contract):
    version: str
    source: Literal["zzshare"]
    status: Literal["ready", "limited", "unavailable"]
    as_of: str | None
    fetched_at: str | None
    point_in_time: bool
    reason: str
    sync: str | None = None
    metrics: list[SentimentMetric]
    loss_gate: LossGate
    cross_check: CrossCheck
    history: list[SentimentPoint] = Field(max_length=60)
    revisions: int = Field(ge=0)
    limitations: list[str]
