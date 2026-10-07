"""Separate, versioned observations; never amendments to a frozen plan."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.services.market_game_ecology_models import FeedbackObservation
from app.services.market_game_models import Contract, Ratio
from app.services.market_game_overseas_models import OverseasContext


class CapitalMetric(Contract):
    score: float | None = Field(default=None, ge=0, le=100)
    raw_value: float | None = None
    sample_size: int = Field(default=0, ge=0)


class CapitalWindow(Contract):
    minutes: Literal[5, 15, 30]
    change: float | None = Field(default=None, alias="return")
    volume_ratio: float | None = None


class CapitalScenario(Contract):
    id: str
    label: str
    evidence: list[str]
    alternative: str
    confirmation: str
    invalidation: str


class CapitalRow(Contract):
    symbol: str
    name: str
    sector: str | None
    is_candidate: bool
    status: str
    reason: str
    as_of: str | None
    windows: list[CapitalWindow]
    participation: CapitalMetric
    support: CapitalMetric
    distribution: CapitalMetric
    reference_price: float | None
    price_bias: float | None
    scenario: CapitalScenario


class CapitalSector(Contract):
    name: str
    sample_size: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    coverage: Ratio
    breadth: Ratio | None
    participation: float | None = Field(ge=0, le=100)
    support: float | None = Field(ge=0, le=100)
    distribution: float | None = Field(ge=0, le=100)
    scenario: str


class CapitalPoint(Contract):
    time: str
    sample_size: int = Field(ge=0)
    participation: float | None = Field(ge=0, le=100)
    support: float | None = Field(ge=0, le=100)
    distribution: float | None = Field(ge=0, le=100)


class CapitalBehavior(Contract):
    input_version: str
    status: Literal["ready", "limited", "unavailable", "stale", "historical"]
    reason: str
    data_date: str | None
    observed_at: str | None
    rows: list[CapitalRow] = Field(max_length=30)
    sectors: list[CapitalSector]
    series: list[CapitalPoint]
    limitations: list[str]


class InstitutionRow(Contract):
    symbol: str
    name: str
    trade_date: str
    range_days: Literal[1, 3]
    org_net_value: float | None
    org_net_rate: float | None
    org_buy_num: int | None
    org_sell_num: int | None


class InstitutionalDisclosures(Contract):
    source: str
    source_label: str
    requested_date: str
    trade_date: str | None
    fetched_at: str
    available_at: str | None
    point_in_time_verified: Literal[False]
    realtime: Literal[False]
    state: Literal["ok", "fallback_prev", "source_unavailable", "no_data", "invalid_data"]
    status: str
    rows: list[InstitutionRow] = Field(max_length=100)
    omitted_count: int = Field(ge=0)
    limitations: list[str]


class CapitalPlanLink(Contract):
    symbol: str
    name: str
    status: Literal["watch", "blocked", "inactive", "unverified"]
    original_max_position: Ratio
    observation_cap: Ratio
    retained_cap: Ratio | None = None
    reason: str
    unchecked_conditions: list[str]

    @model_validator(mode="after")
    def check_cap(self):
        if self.observation_cap > self.original_max_position:
            raise ValueError("资金观察不得提高原计划仓位")
        if self.status != "watch" and self.observation_cap != 0:
            raise ValueError("未核验或否决状态不得保留新增仓位观察额度")
        if self.retained_cap is not None and (self.retained_cap > self.original_max_position
                                             or self.observation_cap > self.retained_cap):
            raise ValueError("本日保留约束不得提高原计划或已收紧仓位")
        if not self.unchecked_conditions:
            raise ValueError("资金观察不能代替原计划条件核验")
        return self


class CapitalObservation(Contract):
    id: str = ""
    report_id: str
    version: Literal["1.0.0"] = "1.0.0"
    created_at: str
    background_date: str
    background_usable: bool
    background_reason: str
    behavior: CapitalBehavior
    disclosures: InstitutionalDisclosures
    plan_status: str
    plan_links: list[CapitalPlanLink]
    entry_authorized: Literal[False] = False
    summary: str
    changes: list[str]
    limitations: list[str]
    overseas: OverseasContext | None = None
    feedback: FeedbackObservation | None = None


class CapitalEvent(Contract):
    id: str
    created_at: str
    observed_at: str | None
    data_date: str | None
    summary: str
    changes: list[str]


class CapitalEnvelope(Contract):
    report_id: str
    latest: CapitalObservation | None
    observations: list[CapitalEvent] = Field(max_length=60)
    refresh_allowed: bool = True
    refresh_reason: str
    auto_refresh_allowed: bool
    history_limit: int = 60
