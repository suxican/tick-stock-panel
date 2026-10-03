"""Versioned HTTP contracts for the research-stage market game plan."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ratio = Annotated[float, Field(ge=0, le=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RiskConfig(Contract):
    total_cap: float = Field(default=0.5, ge=0, le=1, strict=True)
    single_cap: float = Field(default=0.15, ge=0, le=1, strict=True)
    sector_cap: float = Field(default=0.25, ge=0, le=1, strict=True)
    risk_per_trade: float = Field(default=0.005, ge=0.0001, le=0.02, strict=True)
    min_amount: float = Field(default=100_000_000, ge=10_000_000, le=100_000_000_000, strict=True)
    max_candidates: int = Field(default=5, ge=1, le=5, strict=True)


class AnalyzeRequest(Contract):
    as_of: date | None = None
    risk: RiskConfig = Field(default_factory=RiskConfig)


class MarketState(Contract):
    trend: str
    phase: str
    emotion: str
    crowding: str
    change: str


class Allocation(Contract):
    min: Ratio
    max: Ratio
    total_cap: Ratio
    single_cap: Ratio
    sector_cap: Ratio
    risk_per_trade: Ratio
    reason: str


class Hypothesis(Contract):
    id: str
    title: str
    status: Literal["matched", "watch", "inactive"]
    facts: list[str]
    interpretation: str
    alternative: str
    confirm: list[str]
    invalidation: list[str]


class Scenario(Contract):
    horizon: int = Field(ge=1, le=3)
    label: str
    scenario: str
    condition: str
    action: str
    max_position: Ratio


class Sector(Contract):
    name: str
    avg_return: float | None
    breadth: Ratio | None
    status: str
    reason: str


class Candidate(Contract):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str
    sector: str
    mode: str
    role: str
    reference_price: float = Field(gt=0)
    trigger_low: float = Field(gt=0)
    trigger_high: float = Field(gt=0)
    invalidation_price: float = Field(gt=0)
    max_position: Ratio
    stress_loss_pct: float = Field(gt=0)
    holding_days: int = Field(ge=1, le=3)
    evidence: list[str]
    trigger: list[str]
    invalidation: list[str]
    exit_rules: list[str]

    @model_validator(mode="after")
    def check_prices(self):
        if self.invalidation_price >= self.trigger_low or self.trigger_low > self.trigger_high:
            raise ValueError("候选触发与失效价格次序无效")
        return self


class Evidence(Contract):
    id: str
    label: str
    value: float | None
    unit: str
    source: str
    observed_at: str
    available_at: str | None


class GameReport(Contract):
    id: str = ""
    created_at: str = ""
    as_of: str
    cutoff: str
    input_version: str
    rule_version: str
    quality: Literal["ready", "limited", "unavailable"]
    research_only: bool
    summary: str
    market_state: MarketState
    allocation: Allocation
    hypotheses: list[Hypothesis]
    scenarios: list[Scenario]
    sectors: list[Sector] = Field(max_length=3)
    candidates: list[Candidate] = Field(max_length=5)
    evidence: list[Evidence]
    limitations: list[str]

    @model_validator(mode="after")
    def check_allocation(self):
        allocation = self.allocation
        if not 0 <= allocation.min <= allocation.max <= allocation.total_cap:
            raise ValueError("报告总仓位超出约束")
        symbols = [item.symbol for item in self.candidates]
        if len(symbols) != len(set(symbols)):
            raise ValueError("候选股票重复")
        if self.quality == "unavailable" and (self.candidates or allocation.max > 0):
            raise ValueError("不可用数据不能产生新增仓位")
        if sum(item.max_position for item in self.candidates) > allocation.max + 1e-6:
            raise ValueError("候选仓位合计超出总上限")
        sectors: dict[str, float] = {}
        for item in self.candidates:
            if item.max_position > allocation.single_cap + 1e-6:
                raise ValueError("个股仓位超出上限")
            sectors[item.sector] = sectors.get(item.sector, 0) + item.max_position
        if any(value > allocation.sector_cap + 1e-6 for value in sectors.values()):
            raise ValueError("板块仓位超出上限")
        return self
